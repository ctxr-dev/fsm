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

import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Literal

import httpx
import typer

from ctxr.fsm.cli._common import json_or_pretty
from ctxr.fsm.cli.init_cmd import run_init
from ctxr.fsm.cli.install_mcp_cmd import run_install_mcp
from ctxr.fsm.cli.install_memory_cmd import (
    run_install_memory,
    run_install_memory_check,
)
from ctxr.fsm.cli.lifecycle.primitives import (
    pid_is_alive,
    read_active_mcp_file,
    read_pid_file,
)

__all__ = ["ensure", "run_ensure"]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CLIENT_CHOICES: tuple[str, ...] = ("auto", "claude", "codex", "cursor", "none")
_MODE_CHOICES: tuple[str, ...] = ("full", "mcp-only")

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
    stderr are redirected to a per-day log file under
    ``<project_root>/.ctxr-fsm/logs/`` so a later operator can
    inspect what happened during the cold start.

    Returns the spawned pid (for the ensure summary; ownership is
    transferred to the OS — the parent process is free to exit).
    """
    import shutil

    logs_dir = project_root / ".ctxr-fsm" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"supervisor-{time.strftime('%Y%m%d')}.log"
    log_fp = open(log_path, "ab", buffering=0)  # noqa: SIM115

    # Single resolution path: rely on the ``ctxr-fsm`` console script
    # the project's pyproject.toml ships. The previous code had a
    # ``[sys.executable, "-m", "ctxr.fsm.cli", ...]`` construction as
    # a "fallback" but the package has no ``__main__.py`` under
    # ``ctxr.fsm.cli`` — the module form would fail with "No module
    # named ctxr.fsm.cli.__main__", confusing operators in the rare
    # case that the console script is genuinely missing. We surface
    # that case as a clear MissingRequirement-style error instead.
    binary = shutil.which("ctxr-fsm")
    if binary is None:
        raise RuntimeError(
            "ctxr-fsm console script not found on PATH; install the "
            "package with `uv add ctxr-fsm` / `pipx install ctxr-fsm` / "
            "`pip install --user ctxr-fsm` and re-run."
        )
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


def _subsystem_list(mode: str) -> tuple[str, ...]:
    return ("mcp",) if mode == "mcp-only" else ("mcp", "api", "ui")


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


def run_ensure(
    *,
    project_root: Path | None = None,
    client: Literal["auto", "claude", "codex", "cursor", "none"] = "auto",
    mode: Literal["full", "mcp-only"] = "full",
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
    """
    started_at = time.monotonic()
    root = _resolve_project_root(project_root)
    db_path = root / ".ctxr-fsm" / "fsm.db"
    required = _subsystem_list(mode)

    actions: dict[str, str] = {
        "init": "skipped",
        "memory": "skipped",
        "mcp_config": "skipped",
        "supervisor": "skipped",
    }
    mcp_stdio_registered: list[str] = []
    spawn_pid: int | None = None
    failure_detail: str | None = None

    # ---- Step 1: init ----------------------------------------------
    init_needed = not db_path.exists() or not _alembic_at_head(db_path)
    if init_needed:
        if check:
            actions["init"] = "missing"
        else:
            run_init(db_path=db_path, no_memory=True, cwd=root)
            actions["init"] = "applied"
    else:
        actions["init"] = "current"

    # ---- Step 2: memory --------------------------------------------
    # ``client == "none"`` short-circuits memory too (mirrors the
    # mcp_config branch). install_memory's _CLIENT_CHOICES does NOT
    # accept ``"none"`` (it has no equivalent of --no-mcp-config), so
    # calling it would raise typer.BadParameter and the catch-all
    # below would flip the whole ensure status to ``failed`` for what
    # is actually a "the user asked to skip" path.
    if no_memory or client == "none":
        actions["memory"] = "skipped"
    elif check:
        # Delegate to install_memory's pure --check probe so we report
        # the actual on-disk memory state rather than the
        # ``init_needed`` heuristic. Previously a project where init
        # had run but the AI-client memory files had been deleted
        # would silently report ``current``; now the per-client probe
        # surfaces the drift via the rows' ``status`` field.
        try:
            mem_probe = run_install_memory_check(target=root, client=client)
        except Exception as exc:
            actions["memory"] = "failed"
            failure_detail = f"memory check: {type(exc).__name__}: {exc}"
        else:
            mem_results = mem_probe.get("results", [])
            statuses = [r.get("status") for r in mem_results if "status" in r]
            if not statuses:
                # No client detected at all — neither current nor
                # missing applies; report skipped so the top-level
                # status surfaces the upstream missing_init / etc.
                actions["memory"] = "skipped"
            elif all(s == "ok" for s in statuses):
                actions["memory"] = "current"
            else:
                actions["memory"] = "missing"
    else:
        try:
            mem_result = run_install_memory(target=root, client=client)
            # If every per-client result was a "noop", that counts as
            # current rather than applied.
            results = mem_result.get("results", []) if isinstance(mem_result, dict) else []
            if results and all(r.get("action") == "noop" for r in results):
                actions["memory"] = "current"
            else:
                actions["memory"] = "applied"
        except Exception as exc:
            actions["memory"] = "failed"
            failure_detail = f"memory installer: {type(exc).__name__}: {exc}"

    # ---- Step 3: mcp_config ----------------------------------------
    if no_mcp_config or client == "none":
        actions["mcp_config"] = "skipped"
    elif check:
        probe = run_install_mcp(target_dir=root, client=client, check=True)
        statuses = [
            r.get("status")
            for r in probe.get("results", [])
            if "status" in r
        ]
        if not statuses or all(s == "installed" for s in statuses):
            actions["mcp_config"] = "current"
        else:
            actions["mcp_config"] = "missing"
        mcp_stdio_registered = [
            r["client"] for r in probe.get("results", [])
            if r.get("status") == "installed"
        ]
    else:
        try:
            mcp_result = run_install_mcp(target_dir=root, client=client)
            results = mcp_result.get("results", [])
            applied_any = any(
                r.get("action") in ("applied", "would-apply") for r in results
            )
            unchanged_all = bool(results) and all(
                r.get("action") == "unchanged" for r in results
            )
            if applied_any:
                actions["mcp_config"] = "applied"
            elif unchanged_all:
                actions["mcp_config"] = "unchanged"
            elif not results:
                actions["mcp_config"] = "skipped"
            else:
                actions["mcp_config"] = "applied"
            # Every successful or unchanged result counts as registered.
            mcp_stdio_registered = [
                r["client"]
                for r in results
                if r.get("action") in ("applied", "unchanged")
            ]
        except Exception as exc:
            actions["mcp_config"] = "failed"
            failure_detail = f"mcp install: {type(exc).__name__}: {exc}"

    # ---- Step 4: supervisor ----------------------------------------
    # Probe what's up.
    pre_state: dict[str, tuple[bool, int | None, str | None]] = {
        sub: _probe_subsystem_alive(sub, root) for sub in required
    }
    all_up = all(alive for alive, _, _ in pre_state.values())

    if all_up:
        actions["supervisor"] = "reused"
    elif check:
        actions["supervisor"] = "missing"
    else:
        # Spawn a single supervisor that boots whatever is missing.
        spawn_pid = _spawn_supervisor_detached(
            project_root=root, db_path=db_path, mcp_only=(mode == "mcp-only")
        )
        # Wait for the discovery file + healthz of every required
        # subsystem.
        doc = _wait_for_active_mcp(root, subsystems=required, timeout=timeout)
        if doc is None:
            actions["supervisor"] = "failed"
            failure_detail = (
                f"supervisor did not produce a healthy active-mcp.json "
                f"within {timeout:.1f}s (pid={spawn_pid})"
            )
        else:
            actions["supervisor"] = "spawned"

    # ---- Step 5: read discovery file -------------------------------
    doc = read_active_mcp_file(root)
    subsystems_payload: dict[str, Any] = {}
    if doc is not None:
        doc_subs = doc.get("subsystems", {})
        if isinstance(doc_subs, dict):
            for sub in required:
                block = doc_subs.get(sub)
                if isinstance(block, dict):
                    sub_status: str
                    if not check:
                        # Spawned this run? Otherwise reused.
                        if actions["supervisor"] == "spawned":
                            sub_status = "spawned"
                        elif actions["supervisor"] == "reused":
                            sub_status = "reused"
                        elif actions["supervisor"] == "failed":
                            sub_status = "failed"
                        else:
                            sub_status = "ready"
                    else:
                        sub_status = "ready" if pre_state[sub][0] else "missing"
                    subsystems_payload[sub] = {
                        "http_url": block.get("http_url"),
                        "healthz_url": block.get("healthz_url"),
                        "pid": block.get("pid"),
                        "status": sub_status,
                    }
                    if "docs_url" in block:
                        subsystems_payload[sub]["docs_url"] = block["docs_url"]

    # ---- Final status -----------------------------------------------
    status: str
    if failure_detail is not None:
        status = "failed"
    elif check:
        missing_pieces: list[str] = []
        if actions["init"] == "missing":
            missing_pieces.append("init")
        if actions["memory"] == "missing":
            missing_pieces.append("memory")
        if actions["mcp_config"] == "missing":
            missing_pieces.append("mcp-config")
        if actions["supervisor"] == "missing":
            missing_pieces.append("supervisor")
        status = (
            "missing:" + ",".join(missing_pieces) if missing_pieces else "ready"
        )
    else:
        # Did all required subsystems report ready/spawned/reused?
        ok = all(
            subsystems_payload.get(s, {}).get("status")
            in ("ready", "spawned", "reused")
            for s in required
        )
        status = "ready" if ok else "degraded"

    duration_ms = int((time.monotonic() - started_at) * 1000)

    summary: dict[str, Any] = {
        "status": status,
        "project_root": str(root),
        "duration_ms": duration_ms,
        "actions": actions,
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
    except Exception:
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
            "'mcp-only' (just the MCP server; useful for headless CI)."
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
) -> None:
    """Ensure the project is fully bootstrapped + the supervisor is up.

    One-shot, idempotent, fast on the warm path (<500ms). See the
    module docstring for the full algorithm.
    """
    if client not in _CLIENT_CHOICES:
        raise typer.BadParameter(
            f"--client must be one of {_CLIENT_CHOICES!r} (got {client!r})"
        )
    if mode not in _MODE_CHOICES:
        raise typer.BadParameter(
            f"--mode must be one of {_MODE_CHOICES!r} (got {mode!r})"
        )

    effective_json = _json_default() if json_mode is None else json_mode
    summary = run_ensure(
        project_root=project_root,
        client=client,  # type: ignore[arg-type]
        mode=mode,  # type: ignore[arg-type]
        no_memory=no_memory,
        no_mcp_config=no_mcp_config,
        check=check,
        timeout=timeout,
    )
    json_or_pretty(summary, effective_json)

    # Exit non-zero on a failed run so wrapping scripts can react.
    status = summary.get("status", "")
    if isinstance(status, str) and (status == "failed" or status == "degraded"):
        raise typer.Exit(1)
    if check and isinstance(status, str) and status.startswith("missing:"):
        raise typer.Exit(1)


