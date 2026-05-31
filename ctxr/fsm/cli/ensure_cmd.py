"""``ctxr-fsm ensure`` — single bootstrap entry point skills call (W14b).

This command is the **one** thing every ctxr-fsm-driven skill invokes
from its SKILL.md preamble. It is idempotent + fast (<500ms on the
warm path) and self-heals every dimension that could be cold:

1. **init** — ``./.ctxr-fsm/`` + ``fsm.db`` + alembic head.
2. **memory** — FSM-usage principles wired into CLAUDE.md /
   AGENTS.md / .cursor/rules/ (W11's install-memory).
3. **mcp-config** — stdio MCP entry merged into the detected client
   configs (W14d's install-mcp).
4. **supervisor** — MCP (+ API + UI in ``full`` mode) spawned as a
   detached background process, with healthz probed for up to
   ``--timeout`` seconds.

The command's stdout is a single JSON document the skill parses to
discover URLs + verify status. Pretty output is available behind
``--no-json`` for human invocation.

Algorithm summary (per W14b brief)
----------------------------------

1. Resolve project root (cwd walk-up for ``.ctxr-fsm/``; ``--project-root``
   overrides; falls back to cwd).
2. ``init`` step: if DB missing OR alembic head not current → call
   ``run_init``. Idempotent.
3. ``memory`` step: ``run_install_memory(target=project_root,
   client=client)`` unless ``--no-memory``.
4. ``mcp_config`` step: ``run_install_mcp(target_dir=project_root,
   client=client)`` unless ``--no-mcp-config``.
5. ``supervisor`` step: for each of ``{mcp, api, ui}`` (or just
   ``{mcp}`` in mcp-only mode), call ``acquire_singleton``. NOT-up
   subsystems → spawn one ``ctxr-fsm serve`` detached process that
   boots whatever is missing; poll healthz for up to ``--timeout``.
6. Read ``active-mcp.json`` for the final URL set.
7. Emit JSON.

``--check`` bypasses steps 2-3 mutation + the spawn in step 5 (probes
only), returning a per-step ``current``/``missing``/``unchanged``
status.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import typer

from ctxr.fsm.cli._clients import (
    EnsureActionStatus,
    EnsureMode,
    EnsureStatus,
    McpClient,
    McpConfigStatus,
)
from ctxr.fsm.cli._common import json_or_pretty
from ctxr.fsm.cli._render import print_subsystem_table
from ctxr.fsm.cli.init_cmd import run_init
from ctxr.fsm.cli.install_mcp_cmd import run_install_mcp
from ctxr.fsm.cli.install_memory_cmd import run_install_memory
from ctxr.fsm.cli.lifecycle.primitives import (
    pid_is_alive,
    read_active_mcp_file,
    read_pid_file,
    sharded_log_path,
)

__all__ = ["ensure", "run_ensure"]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The tuple forms are kept as a single grep-target for tests and
# wrapping scripts; the enums above are the canonical source.
_CLIENT_CHOICES: tuple[str, ...] = tuple(member.value for member in McpClient)
_MODE_CHOICES: tuple[str, ...] = tuple(member.value for member in EnsureMode)

# Legacy alias map for ``--mode``. W14i renamed the hyphenated wire
# value ``mcp-only`` to the underscored ``mcp_only`` (StrEnum members
# cannot contain hyphens). The hyphenated form is still accepted at the
# CLI boundary so any shipped script / README copy / muscle memory that
# uses the old spelling keeps working; we normalise it into the
# canonical underscored value before validation and emit a one-line
# deprecation warning so the operator knows to update their scripts.
_MODE_ALIASES: dict[str, str] = {
    "mcp-only": EnsureMode.mcp_only.value,
}

# How often (seconds) we poll between healthz attempts while waiting
# for a freshly-spawned supervisor to come up. 100ms is short enough
# to make the warm-path fast (the discovery file lands inside one or
# two polls) and long enough that we don't spin the CPU.
_POLL_INTERVAL_S: float = 0.1


# ---------------------------------------------------------------------------
# Project-root resolution (walk-up from cwd)
# ---------------------------------------------------------------------------


def _walk_up_for_state_dir(start: Path) -> Path | None:
    """Return the nearest ancestor of ``start`` containing ``.ctxr-fsm/``.

    Mirror of the helper in ``cli/_common.py`` and ``mcp/server.py``;
    re-declared here so the ensure command does not pull either of
    those into a hot-path import chain. The three implementations are
    one-screen and trivially kept in sync.
    """
    current = start.resolve()
    for candidate in [current, *current.parents]:
        if (candidate / ".ctxr-fsm").is_dir():
            return candidate
    return None


def _resolve_project_root(explicit: Path | None) -> Path:
    """Resolve the project root the ensure command will operate on."""
    if explicit is not None:
        return explicit.expanduser().resolve()
    found = _walk_up_for_state_dir(Path.cwd())
    if found is not None:
        return found
    return Path.cwd().resolve()


# ---------------------------------------------------------------------------
# Subsystem probing (read-only)
# ---------------------------------------------------------------------------


def _probe_subsystem_alive(name: str, project_root: Path) -> tuple[bool, int | None, str | None]:
    """Return ``(alive, pid, probe_url)`` for ``name``'s singleton state.

    "alive" here means BOTH:

    * The pid recorded in ``.ctxr-fsm/pids/<name>.pid`` is alive
      (per :func:`pid_is_alive`).
    * Its recorded ``probe_url`` answers ``/healthz`` with 200
      (when the subsystem advertises a probe URL — UI omits one
      because Vite has no /healthz route, in which case a live pid
      alone is enough).

    Returning a tuple (not a bool) lets the caller surface diagnostic
    detail in the final JSON without re-reading the file.
    """
    # Construct the pid path manually rather than calling
    # ``pid_file_for`` — that helper has a side effect (creating
    # ``.ctxr-fsm/pids/``). ``run_ensure(check=True)`` MUST never
    # mutate the project tree, so we use the bare path here.
    pid_path = project_root / ".ctxr-fsm" / "pids" / f"{name}.pid"
    record = read_pid_file(pid_path)
    if record is None:
        return False, None, None
    raw_pid = record.get("pid")
    pid = int(raw_pid) if isinstance(raw_pid, int) else None
    probe_url_raw = record.get("probe_url")
    probe_url = probe_url_raw if isinstance(probe_url_raw, str) and probe_url_raw else None

    if pid is None or not pid_is_alive(pid):
        return False, pid, probe_url

    if probe_url is None:
        # Live pid but no probe — match the supervisor's own
        # singleton primitive: a live pid alone is enough for
        # non-HTTP subsystems.
        return True, pid, None

    healthz_url = probe_url.rstrip("/") + "/healthz"
    try:
        resp = httpx.get(healthz_url, timeout=1.0)
    except (httpx.HTTPError, OSError):
        return False, pid, probe_url
    if resp.status_code != 200:
        return False, pid, probe_url
    return True, pid, probe_url


def _alembic_at_head(db_path: Path) -> bool:
    """Return True iff ``alembic_version`` table exists and has a row.

    A missing DB collapses to False (caller will run init). When
    ``alembic_version`` exists with a non-null version, we treat the
    DB as "current" — a strict head-comparison would require loading
    the Alembic script directory and is much heavier than the
    bootstrap path needs to pay on every ensure call. ``run_init``'s
    own ``run_migrations`` is itself idempotent, so a stale revision
    that slips through here will be upgraded on the next genuine
    init call; the ensure fast-path stays cheap.
    """
    if not db_path.exists():
        return False
    import sqlite3

    try:
        con = sqlite3.connect(str(db_path))
    except sqlite3.Error:
        return False
    try:
        try:
            row = con.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()
        except sqlite3.OperationalError:
            return False
    finally:
        con.close()
    return row is not None and row[0] is not None


# ---------------------------------------------------------------------------
# Detached supervisor spawn
# ---------------------------------------------------------------------------


def _spawn_supervisor_detached(
    *, project_root: Path, db_path: Path, mcp_only: bool
) -> int:
    """Spawn ``ctxr-fsm serve`` as a detached background process.

    The supervisor runs in a new session (``start_new_session=True``)
    so a SIGINT in the ensuring shell doesn't take it down. stdout +
    stderr are redirected to a date-sharded log file under
    ``<project_root>/.ctxr-fsm/logs/supervisor/YYYY/MM/DD/`` (see
    :func:`sharded_log_path` for the convention) so a later operator
    can inspect what happened during the cold start AND so the logs
    directory doesn't grow into a flat accumulation that bottlenecks
    the filesystem over months of dev sessions.

    Returns the spawned pid (for the ensure summary; ownership is
    transferred to the OS — the parent process is free to exit).
    """
    log_path = sharded_log_path(
        "supervisor", project_root=project_root, pid=os.getpid()
    )
    log_fp = open(log_path, "ab", buffering=0)  # noqa: SIM115
    cmd = [
        sys.executable or "python3",
        "-m",
        "ctxr.fsm.cli",
        "serve",
        "--mode",
        "dev",
        "--db",
        str(db_path),
    ]
    if mcp_only:
        cmd.append("--mcp-only")

    # ``sys.executable -m ctxr.fsm.cli`` is brittle if the package
    # entry point isn't a module — fall back to the ``ctxr-fsm``
    # console script when sys.executable isn't a usable Python.
    if not sys.executable:
        cmd = ["ctxr-fsm", "serve", "--mode", "dev", "--db", str(db_path)]
        if mcp_only:
            cmd.append("--mcp-only")

    # Some environments don't expose the ``-m ctxr.fsm.cli`` module
    # entry (the project ships only the ``ctxr-fsm`` console script).
    # Probe ``ctxr-fsm`` on PATH first; if found, prefer it.
    import shutil

    binary = shutil.which("ctxr-fsm")
    if binary is not None:
        cmd = [binary, "serve", "--mode", "dev", "--db", str(db_path)]
        if mcp_only:
            cmd.append("--mcp-only")

    proc = subprocess.Popen(
        cmd,
        cwd=str(project_root),
        stdin=subprocess.DEVNULL,
        stdout=log_fp,
        stderr=log_fp,
        start_new_session=True,
        close_fds=True,
    )
    return proc.pid


# ---------------------------------------------------------------------------
# Pure ensure body (used by Typer command + tests + ensure --check)
# ---------------------------------------------------------------------------


def _subsystem_list(mode: EnsureMode) -> tuple[str, ...]:
    """Subsystems the ensure pipeline guarantees up for ``mode``."""
    return ("mcp",) if mode is EnsureMode.mcp_only else ("mcp", "api", "ui")


def _wait_for_active_mcp(
    project_root: Path, *, subsystems: tuple[str, ...], timeout: float
) -> dict[str, Any] | None:
    """Poll for ``active-mcp.json`` to include every required subsystem.

    The supervisor writes the file once MCP's /healthz succeeds. We
    additionally wait for each required subsystem's healthz to pass
    so the returned URLs are guaranteed live (not "supervisor is
    up but API hasn't finished migrating yet").

    Returns the parsed document on success, ``None`` on timeout.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        doc = read_active_mcp_file(project_root)
        if doc is not None:
            doc_subs = doc.get("subsystems", {})
            if isinstance(doc_subs, dict) and all(s in doc_subs for s in subsystems):
                # Probe each one's healthz where applicable.
                all_ready = True
                for sub in subsystems:
                    block = doc_subs.get(sub, {})
                    healthz = block.get("healthz_url") if isinstance(block, dict) else None
                    if healthz is None:
                        # UI has no healthz; pid liveness is the
                        # signal. We already trust the supervisor's
                        # write of the block.
                        continue
                    try:
                        resp = httpx.get(healthz, timeout=1.0)
                        if resp.status_code != 200:
                            all_ready = False
                            break
                    except (httpx.HTTPError, OSError):
                        all_ready = False
                        break
                if all_ready:
                    return doc
        time.sleep(_POLL_INTERVAL_S)
    return None


def _step_init(
    *, db_path: Path, root: Path, check: bool
) -> tuple[EnsureActionStatus, bool]:
    """Run (or probe) the ``init`` step.

    Returns ``(status, init_was_needed)``. The boolean is kept so the
    follow-on ``memory`` step can answer "would memory be missing if
    init has not run yet?" without re-probing the substrate.
    """
    init_needed = not db_path.exists() or not _alembic_at_head(db_path)
    if not init_needed:
        return EnsureActionStatus.current, False
    if check:
        return EnsureActionStatus.missing, True
    run_init(db_path=db_path, no_memory=True, cwd=root)
    return EnsureActionStatus.applied, True


def _step_memory(
    *, root: Path, client: McpClient, no_memory: bool, check: bool, init_needed: bool
) -> tuple[EnsureActionStatus, str | None]:
    """Run (or probe) the ``memory`` step. Returns ``(status, failure_detail)``."""
    if no_memory:
        return EnsureActionStatus.skipped, None
    if check:
        return (
            EnsureActionStatus.current if not init_needed else EnsureActionStatus.missing
        ), None
    try:
        mem_result = run_install_memory(target=root, client=client.value)
    except Exception as exc:
        # Catch-all is deliberate: the ensure surface promises that
        # *every* underlying installer exception becomes a structured
        # ``failed`` summary so the caller (a sub-agent / CI script /
        # the supervisor health probe) never has to handle a Python
        # traceback on stderr. Narrowing to specific exception types
        # would force this layer to keep up with every dependency
        # the installer transitively imports — the opposite of the
        # SRP boundary we want here.
        return EnsureActionStatus.failed, (
            f"memory installer: {type(exc).__name__}: {exc}"
        )
    results = mem_result.get("results", [])
    if results and all(r.get("action") == "noop" for r in results):
        return EnsureActionStatus.current, None
    return EnsureActionStatus.applied, None


def _step_mcp_config(
    *, root: Path, client: McpClient, no_mcp_config: bool, check: bool
) -> tuple[EnsureActionStatus, list[str], str | None]:
    """Run (or probe) the ``mcp_config`` step.

    Returns ``(status, mcp_stdio_registered, failure_detail)``.
    """
    if no_mcp_config or client is McpClient.none:
        return EnsureActionStatus.skipped, [], None

    if check:
        probe = run_install_mcp(target_dir=root, client=client, check=True)
        results = probe.get("results", [])
        statuses = [r.get("status") for r in results if "status" in r]
        installed_value = McpConfigStatus.installed.value
        registered = [
            r["client"]
            for r in results
            if r.get("status") == installed_value
        ]
        if not statuses or all(s == installed_value for s in statuses):
            return EnsureActionStatus.current, registered, None
        return EnsureActionStatus.missing, registered, None

    try:
        mcp_result = run_install_mcp(target_dir=root, client=client)
    except Exception as exc:
        return EnsureActionStatus.failed, [], (
            f"mcp install: {type(exc).__name__}: {exc}"
        )

    results = mcp_result.get("results", [])
    applied_any = any(
        r.get("action") in ("applied", "would-apply") for r in results
    )
    unchanged_all = bool(results) and all(
        r.get("action") == "unchanged" for r in results
    )
    registered = [
        r["client"]
        for r in results
        if r.get("action") in ("applied", "unchanged")
    ]
    if applied_any:
        return EnsureActionStatus.applied, registered, None
    if unchanged_all:
        return EnsureActionStatus.unchanged, registered, None
    if not results:
        return EnsureActionStatus.skipped, registered, None
    return EnsureActionStatus.applied, registered, None


def _final_status(
    *,
    failure_detail: str | None,
    check: bool,
    actions: dict[str, EnsureActionStatus],
    subsystems_payload: dict[str, Any],
    required: tuple[str, ...],
) -> EnsureStatus:
    """Compute the top-level :class:`EnsureStatus` from per-step + per-subsystem state.

    Centralised so the apply path and ``--check`` path share one
    decision tree. The mapping from "which step is missing" to a
    specific ``EnsureStatus.missing_*`` member uses an explicit
    table, not a dynamic ``getattr``, so adding a new ``EnsureStatus``
    member surfaces as a typed test failure rather than silent
    fallthrough.
    """
    if failure_detail is not None:
        return EnsureStatus.failed

    if check:
        missing_to_status: list[tuple[str, EnsureStatus]] = [
            ("init", EnsureStatus.missing_init),
            ("memory", EnsureStatus.missing_memory),
            ("mcp_config", EnsureStatus.missing_mcp_config),
            ("supervisor", EnsureStatus.missing_supervisor),
        ]
        for action_key, missing_status in missing_to_status:
            if actions[action_key] is EnsureActionStatus.missing:
                return missing_status
        return EnsureStatus.ready

    healthy_subsystem_statuses = {
        EnsureActionStatus.applied.value,  # "applied" never used; kept for clarity
        EnsureActionStatus.reused.value,
        EnsureActionStatus.spawned.value,
        "ready",
    }
    ok = all(
        subsystems_payload.get(s, {}).get("status") in healthy_subsystem_statuses
        for s in required
    )
    return EnsureStatus.ready if ok else EnsureStatus.degraded


def run_ensure(
    *,
    project_root: Path | None = None,
    client: McpClient | str = McpClient.auto,
    mode: EnsureMode | str = EnsureMode.full,
    no_memory: bool = False,
    no_mcp_config: bool = False,
    check: bool = False,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Run the ensure pipeline and return the JSON-shaped summary.

    Pure (non-printing) entry point so tests and ``ctxr-fsm ensure``
    share one body. See the module docstring for the full algorithm.

    The returned dict matches the schema documented in the W14b brief:
    ``status / project_root / duration_ms / actions / subsystems``
    + ``mcp_stdio_registered`` (list of client labels).

    ``client`` and ``mode`` accept either typed enum members or raw
    strings; both forms normalise to the enum before dispatch.
    """
    try:
        client_enum = client if isinstance(client, McpClient) else McpClient(client)
    except ValueError as exc:
        raise ValueError(
            f"client must be one of {_CLIENT_CHOICES!r}; got {client!r}"
        ) from exc
    # Accept the legacy hyphenated form (W14i wire-rename) as an alias
    # so programmatic callers do not break either.
    if isinstance(mode, str) and mode in _MODE_ALIASES:
        mode = _MODE_ALIASES[mode]
    try:
        mode_enum = mode if isinstance(mode, EnsureMode) else EnsureMode(mode)
    except ValueError as exc:
        raise ValueError(
            f"mode must be one of {_MODE_CHOICES!r}; got {mode!r}"
        ) from exc

    started_at = time.monotonic()
    root = _resolve_project_root(project_root)
    db_path = root / ".ctxr-fsm" / "fsm.db"
    required = _subsystem_list(mode_enum)

    actions: dict[str, EnsureActionStatus] = {
        "init": EnsureActionStatus.skipped,
        "memory": EnsureActionStatus.skipped,
        "mcp_config": EnsureActionStatus.skipped,
        "supervisor": EnsureActionStatus.skipped,
    }
    mcp_stdio_registered: list[str] = []
    spawn_pid: int | None = None
    failure_detail: str | None = None

    # ---- Step 1: init ----------------------------------------------
    actions["init"], init_needed = _step_init(
        db_path=db_path, root=root, check=check
    )

    # ---- Step 2: memory --------------------------------------------
    actions["memory"], mem_failure = _step_memory(
        root=root,
        client=client_enum,
        no_memory=no_memory,
        check=check,
        init_needed=init_needed,
    )
    if mem_failure is not None:
        failure_detail = mem_failure

    # ---- Step 3: mcp_config ----------------------------------------
    mcp_status, mcp_stdio_registered, mcp_failure = _step_mcp_config(
        root=root, client=client_enum, no_mcp_config=no_mcp_config, check=check
    )
    actions["mcp_config"] = mcp_status
    if mcp_failure is not None and failure_detail is None:
        failure_detail = mcp_failure

    # ---- Step 4: supervisor ----------------------------------------
    # Probe what's up.
    pre_state: dict[str, tuple[bool, int | None, str | None]] = {
        sub: _probe_subsystem_alive(sub, root) for sub in required
    }
    all_up = all(alive for alive, _, _ in pre_state.values())

    if all_up:
        actions["supervisor"] = EnsureActionStatus.reused
    elif check:
        actions["supervisor"] = EnsureActionStatus.missing
    else:
        # Spawn a single supervisor that boots whatever is missing.
        spawn_pid = _spawn_supervisor_detached(
            project_root=root,
            db_path=db_path,
            mcp_only=(mode_enum is EnsureMode.mcp_only),
        )
        # Wait for the discovery file + healthz of every required
        # subsystem.
        doc = _wait_for_active_mcp(root, subsystems=required, timeout=timeout)
        if doc is None:
            actions["supervisor"] = EnsureActionStatus.failed
            failure_detail = (
                f"supervisor did not produce a healthy active-mcp.json "
                f"within {timeout:.1f}s (pid={spawn_pid})"
            )
        else:
            actions["supervisor"] = EnsureActionStatus.spawned

    # ---- Step 5: read discovery file -------------------------------
    doc = read_active_mcp_file(root)
    subsystems_payload: dict[str, Any] = {}
    if doc is not None:
        doc_subs = doc.get("subsystems", {})
        if isinstance(doc_subs, dict):
            supervisor_action = actions["supervisor"]
            for sub in required:
                block = doc_subs.get(sub)
                if not isinstance(block, dict):
                    continue
                sub_status: str
                if check:
                    sub_status = "ready" if pre_state[sub][0] else EnsureActionStatus.missing.value
                elif supervisor_action is EnsureActionStatus.spawned:
                    sub_status = EnsureActionStatus.spawned.value
                elif supervisor_action is EnsureActionStatus.reused:
                    sub_status = EnsureActionStatus.reused.value
                elif supervisor_action is EnsureActionStatus.failed:
                    sub_status = EnsureActionStatus.failed.value
                else:
                    sub_status = "ready"
                subsystems_payload[sub] = {
                    "http_url": block.get("http_url"),
                    "healthz_url": block.get("healthz_url"),
                    "pid": block.get("pid"),
                    "status": sub_status,
                }
                if "docs_url" in block:
                    subsystems_payload[sub]["docs_url"] = block["docs_url"]

    # ---- Final status -----------------------------------------------
    final_status = _final_status(
        failure_detail=failure_detail,
        check=check,
        actions=actions,
        subsystems_payload=subsystems_payload,
        required=required,
    )

    duration_ms = int((time.monotonic() - started_at) * 1000)

    # Serialise enum members to their wire values so the JSON payload
    # is byte-identical to the pre-W14i shape on every step that
    # didn't change.
    actions_wire: dict[str, str] = {
        key: status.value for key, status in actions.items()
    }
    summary: dict[str, Any] = {
        "status": final_status.value,
        "project_root": str(root),
        "duration_ms": duration_ms,
        "actions": actions_wire,
        "mcp_stdio_registered": mcp_stdio_registered,
        "subsystems": subsystems_payload,
    }
    if spawn_pid is not None:
        summary["spawned_supervisor_pid"] = spawn_pid
    if failure_detail is not None:
        summary["failure_detail"] = failure_detail
    return summary


# ---------------------------------------------------------------------------
# Typer entry point
# ---------------------------------------------------------------------------


def _json_default() -> bool:
    """Default the --json flag to True when stdout is not a TTY.

    Skills consume the ensure output programmatically; defaulting to
    JSON on a pipe avoids forcing every caller to remember
    ``--json``. Interactive terminals get the pretty-printed default.
    """
    try:
        return not sys.stdout.isatty()
    except (AttributeError, OSError):
        # Some test runners replace sys.stdout with a buffer that
        # lacks ``isatty`` (AttributeError) and certain CI sandboxes
        # raise OSError on the fileno() call ``isatty`` makes
        # internally. Either way we default to JSON because the
        # absence of a TTY is the trigger for it in the first place.
        return True


def ensure(
    project_root: Path | None = typer.Option(  # noqa: B008 — typer sentinel
        None,
        "--project-root",
        help=(
            "Project root to ensure. Defaults to walk-up from the "
            "current directory looking for .ctxr-fsm/; falls back to "
            "cwd when no ancestor matches."
        ),
        resolve_path=True,
    ),
    client: str = typer.Option(
        "auto",
        "--client",
        help=(
            "MCP client config(s) to register against: 'auto' (detect "
            "every matching client config in the project + user), "
            "'claude', 'codex', 'cursor', or 'none' (skip)."
        ),
    ),
    mode: str = typer.Option(
        "full",
        "--mode",
        help=(
            "Bootstrap scope: 'full' (MCP + API + UI; default) or "
            "'mcp_only' (just the MCP server; useful for headless CI). "
            "The legacy hyphenated form 'mcp-only' is still accepted "
            "and silently normalised to 'mcp_only' with a deprecation "
            "warning."
        ),
    ),
    no_memory: bool = typer.Option(
        False,
        "--no-memory",
        help="Skip the install-memory step (CLAUDE.md / AGENTS.md patches).",
    ),
    no_mcp_config: bool = typer.Option(
        False,
        "--no-mcp-config",
        help=(
            "Skip the install-mcp step (mostly for tests; rarely "
            "user-facing)."
        ),
    ),
    check: bool = typer.Option(
        False,
        "--check",
        help=(
            "Read-only probe: report per-step status without taking "
            "action; never spawns processes; never mutates files."
        ),
    ),
    timeout: float = typer.Option(
        15.0,
        "--timeout",
        help=(
            "Wall-clock seconds the ensure command waits for the "
            "supervisor's healthz before returning status='failed'. "
            "Default 15s covers a cold uv-run + FastMCP import graph."
        ),
        min=1.0,
        max=300.0,
    ),
    json_mode: bool | None = typer.Option(
        None,
        "--json/--no-json",
        help=(
            "Force JSON or pretty-printed output. Default: JSON when "
            "stdout is not a TTY, pretty otherwise."
        ),
    ),
    no_table: bool = typer.Option(
        False,
        "--no-table",
        help=(
            "Skip the Rich subsystem URL table in TTY non-JSON mode. "
            "The actions-summary dict still prints. JSON mode is "
            "unaffected."
        ),
    ),
) -> None:
    """Ensure the project is fully bootstrapped + the supervisor is up.

    One-shot, idempotent, fast on the warm path (<500ms). See the
    module docstring for the full algorithm.
    """
    if client not in _CLIENT_CHOICES:
        raise typer.BadParameter(
            f"--client must be one of {_CLIENT_CHOICES!r} (got {client!r})"
        )
    if mode in _MODE_ALIASES:
        canonical = _MODE_ALIASES[mode]
        typer.echo(
            f"warning: --mode {mode!r} is the legacy hyphenated form; "
            f"please pass {canonical!r} instead. Accepting it for now.",
            err=True,
        )
        mode = canonical
    if mode not in _MODE_CHOICES:
        raise typer.BadParameter(
            f"--mode must be one of {_MODE_CHOICES!r} (got {mode!r})"
        )

    effective_json = _json_default() if json_mode is None else json_mode
    summary = run_ensure(
        project_root=project_root,
        client=client,
        mode=mode,
        no_memory=no_memory,
        no_mcp_config=no_mcp_config,
        check=check,
        timeout=timeout,
    )
    json_or_pretty(summary, effective_json)

    # W14j: in TTY non-JSON mode, follow the actions-summary dict
    # with the Rich subsystem table so a human running ``ensure``
    # interactively sees the URLs they almost certainly came here for
    # (FastAPI + Swagger + UI + MCP). ``--no-table`` opts out for the
    # rare caller that wants pretty output WITHOUT the table; JSON
    # mode is unaffected (machine consumers stay byte-identical).
    #
    # The table is rendered from the ensure summary's own
    # ``subsystems`` block (rather than re-reading ``active-mcp.json``)
    # so the ``status`` column reflects the ``ready`` / ``reused`` /
    # ``spawned`` decision ensure just made — that's the operator's
    # mental model of "what happened" and matches the JSON shape the
    # same call returned. When the block is empty (``--check`` against
    # a cold project, or a failed spawn) the table is suppressed since
    # there is nothing to show.
    if not effective_json and not no_table:
        try:
            tty = sys.stdout.isatty()
        except (AttributeError, OSError):
            tty = False
        if tty:
            root = _resolve_project_root(project_root)
            subsystems_block = summary.get("subsystems") or {}
            if isinstance(subsystems_block, dict) and subsystems_block:
                print_subsystem_table(
                    {"subsystems": subsystems_block}, project_root=root
                )
                # Discoverability tip (W16): operators reach for
                # ``urls``-style shortcuts to "show me where everything
                # is" without remembering ``doctor`` or scrolling
                # through ensure's full output every time.
                typer.echo(
                    "\nTip: run `ctxr-fsm urls` any time to reprint this table.",
                    err=False,
                )

    # Exit non-zero on a failed / degraded run, or on --check that
    # surfaced any ``missing_*`` status, so wrapping scripts can react.
    status_raw = summary.get("status", "")
    try:
        status = EnsureStatus(status_raw) if isinstance(status_raw, str) else None
    except ValueError:
        status = None
    if status is None:
        return
    if status in {EnsureStatus.failed, EnsureStatus.degraded}:
        raise typer.Exit(1)
    missing_statuses = {
        EnsureStatus.missing_init,
        EnsureStatus.missing_supervisor,
        EnsureStatus.missing_mcp_config,
        EnsureStatus.missing_memory,
    }
    if check and status in missing_statuses:
        raise typer.Exit(1)


