"""Unified ``ctxr-fsm serve`` supervisor (W7).

This module orchestrates the long-running development trio — the MCP
server (HTTP/SSE transport), the FastAPI server, and (in ``dev`` mode)
the Vite UI dev server — as a single supervised process tree rooted at
``ctxr-fsm serve``. It replaces the W3 stub by giving operators a
single command to ``Ctrl-C`` and a single banner that prints the three
URLs they actually need.

Design notes
------------

* **Single task group.** Every child process and every long-lived
  background task (file watcher, signal scope, health probes) lives
  inside one ``anyio.create_task_group`` so a SIGINT cancels the lot
  in one atomic move. The supervisor never spawns "loose" threads or
  background tasks outside the group — that would defeat the unified
  cancellation contract the operator expects from Ctrl-C.

* **Singleton-first, spawn-second.** Each subsystem name (``mcp``,
  ``api``, ``ui``) goes through :func:`acquire_singleton` before we
  consider spawning it. A :class:`ReusedSubsystem` return is the
  supervisor's signal to *skip* the spawn entirely and adopt the
  pre-existing instance — the W7 brief explicitly calls this out so
  two ``ctxr-fsm serve`` invocations in the same checkout never fight
  over the same ports.

* **Captive stdio for child output.** The MCP and API children run
  with stdio captured so the supervisor can prefix each line with the
  child name and forward to its own stderr. The UI child inherits
  stdio directly because Vite's banner contains ANSI cursor moves and
  link-clickable URLs that look better unmangled.

* **MCP transport choice.** We spawn the MCP child with
  ``--transport http`` rather than stdio because the supervisor is not
  acting as an MCP client — it's just a process manager. Operators
  who want a stdio MCP for their Claude Code session keep running
  ``ctxr-fsm mcp`` separately; the supervisor's MCP child exists for
  HTTP-based callers (other agents, scripted tools, the W6 UI's MCP
  introspection panel).

* **Dev reload.** In ``dev`` mode we run
  :func:`watchfiles.awatch` over ``<project_root>/ctxr/fsm`` as a
  sibling task. File changes are debounced to 500ms (watchfiles'
  built-in step) and trigger a graceful drain + respawn of the MCP and
  API children. The UI child has its own hot-reload (Vite's HMR) so
  the supervisor never restarts it on a source change.

* **Health probes are best-effort.** After each spawn we poll
  ``GET <probe_url>/healthz`` for up to ~5s. A non-200 (or no answer
  at all) logs an error but does *not* take the supervisor down —
  the child may have legitimate reasons to be slow (cold cache, large
  migration) and the operator can always observe the child's stderr.

* **Drain budget.** SIGTERM is sent first; if the child does not
  reach ``returncode`` within ``drain_timeout`` (5s) we escalate to
  ``kill()``. The supervisor itself then waits up to an additional 5s
  grace before returning. This matches the contract advertised in the
  banner help text and keeps a stuck child from hanging the shell.

* **No daemonisation.** Even in ``prod`` mode the supervisor stays in
  the foreground — daemonisation is the operator's job (systemd,
  ``nohup``, a container's PID 1). What ``prod`` does turn off is the
  watchfiles reload loop and the verbose per-line stdio bridging.
"""

from __future__ import annotations

import functools
import os
import signal
import sys
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

import anyio
import httpx
from watchfiles import awatch

from ctxr.fsm.cli._render import print_subsystem_table
from ctxr.fsm.cli.lifecycle.primitives import (
    PidLock,
    ReusedSubsystem,
    acquire_singleton,
    now_iso_ms,
    pick_port,
    pid_file_for,
    read_active_mcp_file,
    read_pid_file,
    recall_port,
    release_singleton,
    remember_active_mcp_file,
    remember_port,
    remove_active_mcp_file,
    write_pid_file,
)
from ctxr.fsm.sqlite.drift import (
    DRIFT_DISABLED_ENV_VAR,
    DriftConfig,
    drift_detector_loop,
)

__all__ = ["main", "run_supervisor"]


def _locate_package_ui_dir() -> Path | None:
    """Resolve the fsm package's own ``ui/`` source tree.

    The UI (Vite + Preact + Tailwind v4, W6) is part of the ctxr-fsm
    package, not the consumer project. We need its on-disk path so the
    supervisor can spawn ``vite`` with the right ``cwd``. Two layouts
    we have to handle:

    * **Sibling-linked / editable / sdist install** — the package
      lives at ``<repo>/ctxr/fsm/`` (PEP 420 namespace package) and
      the UI lives at ``<repo>/ui/``. ``Path(__file__).resolve()``
      walks ``cli/lifecycle/supervisor.py → cli/ → fsm/ → ctxr/ →
      <repo>``, so the root is ``parents[4]``.
    * **Wheel install** — the wheel's data section lands ``ui/`` next
      to ``ctxr/`` inside the site-packages tree. Same parents[4]
      resolution lands there.

    Returns the directory if it contains a ``package.json``; otherwise
    returns ``None`` and the caller logs the diagnostic + skips UI.
    """
    # supervisor.py lives at <root>/ctxr/fsm/cli/lifecycle/supervisor.py
    # → parents: [lifecycle, cli, fsm, ctxr, <root>] = parents[4].
    candidate = Path(__file__).resolve().parents[4] / "ui"
    if (candidate / "package.json").is_file():
        return candidate
    return None


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# How long to wait for a child to exit after SIGTERM before we escalate
# to ``kill()``. Five seconds is long enough for uvicorn's graceful
# shutdown (in-flight requests + lifespan teardown) and short enough
# that an operator who hit Ctrl-C twice never feels the shell hang.
_DRAIN_TIMEOUT_SECONDS: float = 5.0

# Extra wall-clock budget after the per-child drain finishes, before
# the supervisor itself returns. Lets any final stderr flush land in
# the operator's terminal so a crash trace is not truncated.
_FINAL_GRACE_SECONDS: float = 5.0

# Maximum total seconds spent polling ``/healthz`` after a spawn.
_HEALTH_PROBE_BUDGET_SECONDS: float = 5.0

# Polling interval inside the health-probe budget. 250ms is fast
# enough that a fast-starting child is detected in one tick and slow
# enough that we never DoS our own ``/healthz`` endpoint.
_HEALTH_PROBE_INTERVAL_SECONDS: float = 0.25

# Single-shot HTTP timeout for each ``/healthz`` poll. Short because
# both endpoints are on ``127.0.0.1``; anything slower than this is a
# hung process the next poll will reveal.
_HEALTH_PROBE_TIMEOUT_SECONDS: float = 1.0

# watchfiles debounce window for the dev-mode reload. 500ms collapses
# a typical "save several files at once" event into one drain/respawn
# cycle, which keeps the operator's terminal readable.
_WATCH_DEBOUNCE_MS: int = 500

# Default preferred ports. We try these first via :func:`pick_port`
# so the URLs in operator docs stay stable across restarts; on
# collision the kernel picks an ephemeral one and we remember the
# choice so subsequent restarts reuse it.
_DEFAULT_PREFERRED_PORTS: dict[str, int] = {
    "mcp": 8770,
    "api": 8765,
    "ui": 5173,
}


# ---------------------------------------------------------------------------
# Small helpers (logging, banners, child stdio bridge)
# ---------------------------------------------------------------------------


def _log(line: str) -> None:
    """Write a supervisor log line to stderr with a stable prefix.

    Centralised so every operator-visible message uses the exact same
    ``[ctxr-fsm supervisor]`` prefix the W7 brief documents; tests
    that grep the supervisor's stderr can therefore key on a single
    literal.
    """
    sys.stderr.write(line if line.endswith("\n") else line + "\n")
    sys.stderr.flush()


def _probe_url_for(port: int) -> str:
    """Return the canonical ``http://127.0.0.1:<port>`` probe URL.

    We always bind to ``127.0.0.1`` (never ``0.0.0.0``) so the probe
    URL is unambiguous and the dev loop stays safe-by-default.
    """
    return f"http://127.0.0.1:{port}"


async def _poll_healthz(probe_url: str) -> bool:
    """Poll ``<probe_url>/healthz`` until 200 or the budget runs out.

    Returns ``True`` on a 200 response, ``False`` otherwise. We swallow
    every exception (transport errors, DNS failures, timeouts) because
    the budget loop's job is to absorb all of those into the same
    binary outcome — the caller only cares whether the child is
    answering.
    """
    deadline = anyio.current_time() + _HEALTH_PROBE_BUDGET_SECONDS
    url = probe_url.rstrip("/") + "/healthz"
    async with httpx.AsyncClient(timeout=_HEALTH_PROBE_TIMEOUT_SECONDS) as client:
        while anyio.current_time() < deadline:
            try:
                resp = await client.get(url)
                if resp.status_code == 200:
                    return True
            except (httpx.HTTPError, OSError):
                # Expected during the warmup window — the child may
                # still be binding its socket. Fall through to the
                # sleep and try again.
                pass
            await anyio.sleep(_HEALTH_PROBE_INTERVAL_SECONDS)
    return False


async def _bridge_stream(name: str, stream: anyio.abc.ByteReceiveStream | None) -> None:
    """Forward a child process's captured byte stream to supervisor stderr.

    Each line is prefixed with ``[<name>]`` so an operator watching the
    aggregated output can attribute each line to its source child. We
    decode with ``errors="replace"`` because child processes can (and
    do) emit malformed UTF-8 (especially Vite's box-drawing characters
    when run under an unusual ``LANG``), and a decode crash on the
    bridge would silently drop subsequent output.
    """
    if stream is None:
        return
    buffer = b""
    try:
        async for chunk in stream:
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                text = line.decode("utf-8", errors="replace")
                _log(f"[{name}] {text}")
    except anyio.EndOfStream:
        pass
    except anyio.ClosedResourceError:
        pass
    finally:
        if buffer:
            text = buffer.decode("utf-8", errors="replace")
            _log(f"[{name}] {text}")


# ---------------------------------------------------------------------------
# Port + singleton resolution
# ---------------------------------------------------------------------------


def _resolve_port(name: str, *, project_root: Path) -> int:
    """Pick a port for ``name``, preferring the remembered one.

    Resolution order:

    1. ``recall_port(name)`` — the last successful port we used.
       Honoured first so URLs in operator docs and bookmarks stay
       stable across restarts.
    2. ``_DEFAULT_PREFERRED_PORTS[name]`` — the canonical default.
    3. Whatever the kernel hands us via ephemeral allocation.

    The picked port is *not* remembered here — we wait until the child
    has actually bound it (after the health probe) so a failed boot
    doesn't poison the cache.
    """
    remembered = recall_port(name, project_root=project_root)
    preferred = remembered if remembered is not None else _DEFAULT_PREFERRED_PORTS.get(name)
    return pick_port(preferred)


# ---------------------------------------------------------------------------
# Subsystem spawn helpers
# ---------------------------------------------------------------------------


async def _spawn_child(
    name: str,
    cmd: list[str],
    *,
    cwd: Path | None,
    env: dict[str, str] | None,
    inherit_stdio: bool,
) -> anyio.abc.Process:
    """Open a child process via :func:`anyio.open_process`.

    ``inherit_stdio=True`` is used for the UI child so Vite's
    ANSI-coloured banner reaches the operator's terminal untouched.
    Otherwise stdio is captured and bridged by :func:`_bridge_stream`
    so we can prefix each line with the child name.

    The returned :class:`anyio.abc.Process` MUST be wrapped in an
    ``async with`` block (or its lifetime managed via
    ``await proc.aclose()``) by the caller so the file descriptors
    are reclaimed deterministically.
    """
    if inherit_stdio:
        # Inheriting means we hand the parent's actual fds to the
        # child; cancellation of the supervisor task group still gets
        # SIGTERM to the child via :meth:`Process.terminate` so we
        # don't lose the shutdown contract.
        return await anyio.open_process(
            cmd,
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            stdin=None,
            stdout=None,
            stderr=None,
        )
    return await anyio.open_process(
        cmd,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
    )


async def _drain_child(name: str, proc: anyio.abc.Process) -> None:
    """Send SIGTERM, then SIGKILL if the child overruns the drain budget.

    Idempotent: a child that has already exited is a no-op on every
    branch. We catch :class:`ProcessLookupError` because Python's
    :mod:`subprocess` raises it when signalling an already-reaped pid
    on some platforms — that's a benign race we always want to absorb.
    """
    if proc.returncode is not None:
        return
    with suppress(ProcessLookupError):
        proc.terminate()
    with anyio.move_on_after(_DRAIN_TIMEOUT_SECONDS):
        await proc.wait()
    if proc.returncode is None:
        _log(f"[ctxr-fsm supervisor] {name} did not drain in {_DRAIN_TIMEOUT_SECONDS:.0f}s; killing.")
        with suppress(ProcessLookupError):
            proc.kill()
        with anyio.move_on_after(_FINAL_GRACE_SECONDS):
            await proc.wait()


# ---------------------------------------------------------------------------
# Subsystem command builders
# ---------------------------------------------------------------------------


def _mcp_cmd(*, port: int, db_path: Path | None) -> list[str]:
    """Build the argv for the MCP child.

    We always force ``--transport http`` so the supervisor doesn't
    need to act as an MCP client over the child's stdio. The bind
    host is ``127.0.0.1`` to match :func:`_probe_url_for`.
    """
    cmd = [
        "uv",
        "run",
        "ctxr-fsm",
        "mcp",
        "--transport",
        "http",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    if db_path is not None:
        cmd.extend(["--db", str(db_path)])
    return cmd


def _api_cmd(*, port: int, db_path: Path | None) -> list[str]:
    """Build the argv for the API child.

    We deliberately reuse ``ctxr-fsm api`` rather than booting uvicorn
    in-process so the supervisor's view of the API matches every other
    way an operator might launch it — one boot sequence, one place to
    edit.
    """
    cmd = [
        "uv",
        "run",
        "ctxr-fsm",
        "api",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    if db_path is not None:
        cmd.extend(["--db", str(db_path)])
    return cmd


def _ui_cmd(*, port: int) -> list[str]:
    """Build the argv for the UI child (Vite dev server).

    The leading ``--`` is npm's pass-through marker; everything after
    it is handed verbatim to the underlying ``vite`` invocation.

    ``--host 127.0.0.1`` pins the bind to IPv4 loopback so it matches
    the supervisor's probe URL (``_probe_url_for`` returns
    ``http://127.0.0.1:<port>``) and the e2e fixture's poll target.
    Vite's default bind is ``localhost``, which on Linux CI runners
    resolves first to ``::1`` (IPv6) and rejects IPv4 connections with
    ConnectionRefused, leaving the e2e fixture waiting on a port the
    child never opened on that address.
    """
    return [
        "npm",
        "run",
        "dev",
        "--",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]


# ---------------------------------------------------------------------------
# Acquisition + boot of a single subsystem
# ---------------------------------------------------------------------------


def _record_child_pid(
    lock: PidLock, *, child_pid: int, project_root: Path
) -> PidLock:
    """Overwrite the on-disk pid record with ``child_pid`` and return a new lock.

    :func:`acquire_singleton` had to write the supervisor's own
    ``os.getpid()`` *before* the spawn so a racing second supervisor
    would see the slot occupied. After the spawn, the pid we actually
    want exposed (in ``doctor``, in the reload-loop integration test,
    in a stray-terminal ``kill <pid>``) is the *child's* pid, so we
    rewrite the file with that pid in place. The returned
    :class:`PidLock` mirrors what's on disk so :func:`release_singleton`'s
    "iff we still own it" check still matches.
    """
    pid_path = pid_file_for(lock.name, project_root=project_root)
    new_lock = PidLock(
        name=lock.name,
        pid=child_pid,
        probe_url=lock.probe_url,
        acquired_at=lock.acquired_at,
    )
    write_pid_file(pid_path, asdict(new_lock))
    return new_lock


async def _boot_subsystem(
    name: str,
    *,
    project_root: Path,
    db_path: Path | None,
    task_group: anyio.abc.TaskGroup,
    is_ui: bool = False,
) -> tuple[PidLock | None, int | None, anyio.abc.Process | None, bool]:
    """Acquire the singleton for ``name`` and (maybe) spawn the child.

    Returns ``(lock, port, proc, healthz_ok)`` where any element except
    ``healthz_ok`` may be ``None``:

    * ``lock is None`` when :func:`acquire_singleton` returned a
      :class:`ReusedSubsystem` — we skipped spawning entirely.
    * ``proc is None`` likewise indicates "reuse" (no child of ours).
    * ``port`` is the bound port whether we spawned or reused; the
      banner needs both cases.
    * ``healthz_ok`` is ``True`` on the reuse path (the singleton
      probe just succeeded) or whenever our own ``/healthz`` poll
      returned 200. ``False`` for spawn paths where the probe ran out
      its budget. UI children skip the probe entirely (Vite has no
      ``/healthz``) and the value collapses to ``True`` so callers can
      treat "no probe expected" as a non-failure.

    On the spawn path we:

    1. Pick a port (preferring the remembered one).
    2. Acquire the singleton with that port's probe URL recorded.
    3. Spawn the child as a managed subprocess in ``task_group``.
    4. Start the stdio bridge tasks (captured stdio only).
    5. Poll ``/healthz`` for up to ~5s and log the outcome.
    6. ``remember_port`` only after the health probe ran (success or
       not — the bind itself proves the port is real).
    """
    port = _resolve_port(name, project_root=project_root)
    probe_url = _probe_url_for(port)
    acq = acquire_singleton(name, project_root=project_root, probe_url=probe_url)

    if isinstance(acq, ReusedSubsystem):
        _log(
            f"[ctxr-fsm supervisor] {name} already running "
            f"(pid={acq.existing_pid}, url={acq.probe_url}); skipping spawn."
        )
        # Even on reuse the operator wants the URL in the banner, so
        # we return the existing port (parsed back from the probe URL
        # to avoid divergence with the live singleton). Reuse implies
        # the singleton's own /healthz probe just succeeded, so the
        # ``healthz_ok`` channel collapses to True without needing
        # a second poll here.
        try:
            reused_port = int(acq.probe_url.rsplit(":", 1)[-1].rstrip("/"))
        except (ValueError, AttributeError):
            reused_port = port
        return None, reused_port, None, True

    # Build the right command for this subsystem.
    if name == "mcp":
        cmd = _mcp_cmd(port=port, db_path=db_path)
        cwd: Path | None = None
        env: dict[str, str] | None = None
        inherit = False
    elif name == "api":
        cmd = _api_cmd(port=port, db_path=db_path)
        cwd = None
        env = None
        inherit = False
    elif name == "ui":
        # The UI is a Vite dev server that ships INSIDE the ctxr-fsm
        # package itself (W6 — ``fsm/ui/``), NOT in the consumer
        # project. Consumer projects never have their own ``ui/`` tree
        # because the UI is the package's UI: the operator sees the
        # same dashboard regardless of which project they pointed
        # ``ctxr-fsm`` at. We locate the package's ui/ via
        # :func:`_locate_package_ui_dir` (resolves the source tree
        # whether ctxr-fsm is sibling-linked, editable-installed, or
        # site-packages-installed) and spawn the Vite dev server from
        # THERE. The dev server then reads its config + node_modules
        # from the package and serves SSE/API proxying against the
        # consumer's API child via ``VITE_API_PORT`` in env.
        ui_cwd = _locate_package_ui_dir()
        if ui_cwd is None or not (ui_cwd / "package.json").is_file():
            _log(
                "[ctxr-fsm supervisor] ui skipped: could not locate the "
                "package-owned ui/ directory (looked relative to "
                f"ctxr.fsm.__file__={Path(__file__).resolve()}). The UI "
                "ships inside the ctxr-fsm package; if you installed via "
                "a wheel that excluded the ui/ subtree, reinstall with "
                "the source distribution or use --mode mcp-only to "
                "silence this advisory."
            )
            release_singleton(acq, project_root=project_root)
            return None, None, None, True
        cmd = _ui_cmd(port=port)
        cwd = ui_cwd
        env = os.environ.copy()
        # The Vite config reads VITE_API_PORT at startup to wire the
        # /api/v1 proxy target at the SUPERVISOR's api port for THIS
        # consumer project. That's the bridge: code from the package,
        # data routed at the consumer's API.
        api_port = recall_port("api", project_root=project_root)
        if api_port is not None:
            env["VITE_API_PORT"] = str(api_port)
        # Pass the consumer's project root through so the UI can label
        # the dashboard with "you're looking at <consumer project>".
        env["CTXR_FSM_PROJECT_ROOT"] = str(project_root)
        inherit = True
    else:
        # Defensive: any future subsystem must add an arm here. Crash
        # loudly rather than spawn a wrong command.
        raise ValueError(f"unknown subsystem name: {name!r}")

    proc = await _spawn_child(name, cmd, cwd=cwd, env=env, inherit_stdio=inherit)

    # ``acquire_singleton`` had to write the pid file *before* the
    # spawn so a racing second supervisor would see the slot taken.
    # That puts the supervisor's own pid on disk, but operators (and
    # the reload-loop integration tests) want the *child* pid — that's
    # the process they would send SIGTERM to from a stray terminal, and
    # it's the value that has to change visibly across a respawn. So we
    # overwrite the file in place now that ``proc.pid`` is known, keeping
    # every other field of :class:`PidLock` intact. The returned lock
    # mirrors what's on disk so :func:`release_singleton`'s "iff we
    # still own it" check keeps matching.
    acq = _record_child_pid(acq, child_pid=proc.pid, project_root=project_root)

    # Bridge stdio if captured. UI inherits, so its streams are None
    # and the bridge tasks would no-op anyway — we skip starting them
    # for clarity.
    if not inherit:
        task_group.start_soon(_bridge_stream, name, proc.stdout)
        task_group.start_soon(_bridge_stream, name, proc.stderr)

    # Health probe (best-effort). We only run it for HTTP children;
    # the UI's "ready" signal is Vite's banner, not a /healthz route.
    healthz_ok = True
    if not is_ui:
        healthz_ok = await _poll_healthz(probe_url)
        if not healthz_ok:
            _log(
                f"[ctxr-fsm supervisor] {name} did not answer /healthz within "
                f"{_HEALTH_PROBE_BUDGET_SECONDS:.0f}s — continuing anyway."
            )

    # The bind has happened (the child is running); record the port so
    # the next restart prefers it.
    remember_port(name, port, project_root=project_root)

    return acq, port, proc, healthz_ok


# ---------------------------------------------------------------------------
# Dev-mode reload loop
# ---------------------------------------------------------------------------


async def _reload_loop(
    *,
    project_root: Path,
    db_path: Path | None,
    task_group: anyio.abc.TaskGroup,
    children: dict[str, anyio.abc.Process | None],
    locks: dict[str, PidLock | None],
) -> None:
    """Watch ``ctxr/fsm`` and drain+respawn MCP+API on every change.

    The watcher debounces at 500ms (watchfiles' built-in ``step``) so
    a "save five files at once" event collapses into one cycle. Each
    cycle:

    1. Logs the triggering path.
    2. SIGTERMs the existing MCP + API children and waits for drain.
    3. Releases their singleton pid files so the new children can
       claim them without a stale-record race.
    4. Spawns fresh MCP + API children via :func:`_boot_subsystem`.
    5. Updates the shared ``children`` / ``locks`` maps in place so
       the shutdown path (run on Ctrl-C) targets the new pids.

    The UI child is never restarted here — Vite's HMR handles source
    changes on its own side, and a UI restart would lose the
    operator's open DevTools session.
    """
    watch_root = project_root / "ctxr" / "fsm"
    if not watch_root.exists():
        _log(
            f"[ctxr-fsm supervisor] watch root {watch_root} missing; "
            f"reload loop disabled."
        )
        return

    async for changes in awatch(watch_root, step=_WATCH_DEBOUNCE_MS):
        # Render *one* representative path so the log line is short.
        # ``changes`` is a set of ``(Change, path)`` tuples; we surface
        # the first one alphabetically for stable test output.
        sample_path = sorted(p for _change, p in changes)[0] if changes else "(unknown)"
        _log(
            f"[ctxr-fsm supervisor] file change detected ({sample_path}); "
            f"draining mcp + api..."
        )

        for name in ("mcp", "api"):
            proc = children.get(name)
            if proc is not None:
                await _drain_child(name, proc)
            lock = locks.get(name)
            if lock is not None:
                release_singleton(lock, project_root=project_root)
            children[name] = None
            locks[name] = None

        # Respawn. We reuse the same boot helper so the new children
        # get the same health-probe + port-memory treatment as the
        # initial boot.
        for name in ("mcp", "api"):
            new_lock, _new_port, new_proc, _ok = await _boot_subsystem(
                name,
                project_root=project_root,
                db_path=db_path,
                task_group=task_group,
            )
            children[name] = new_proc
            locks[name] = new_lock

        _log("[ctxr-fsm supervisor] respawned mcp + api")

        # After respawn the MCP child has a new pid + (possibly) port;
        # rewrite the discovery file so any skill that just spawned a
        # reader between drain and respawn does not latch onto the now-
        # dead pid. We re-read the supervisor's pid files for the truth
        # of which pid currently owns each subsystem (the reload above
        # rewrote them via ``_record_child_pid``).
        _publish_active_mcp_file_from_disk(project_root=project_root)


# ---------------------------------------------------------------------------
# Signal scope
# ---------------------------------------------------------------------------


async def _drift_detector_task(db_path: Path) -> None:
    """Open a :class:`Project` and run the W12 drift detector loop.

    The supervisor owns a long-lived background task that opens its
    own :class:`Project` against ``db_path`` and drives
    :func:`drift_detector_loop` for the lifetime of the supervisor.
    A separate Project handle (rather than reusing the MCP / API
    child's handle, which lives in a different process) is required
    because the drift detector talks directly to the SQLite
    substrate; we cannot tunnel through the children's HTTP
    surfaces without inventing a new bridge.

    The loop honours the :data:`DRIFT_DISABLED_ENV_VAR` env var
    before the Project is opened, so flipping the kill switch
    skips the engine bind entirely — useful for ops who need to
    stop the loop without paying the migration cost on every
    reload cycle.
    """
    if os.environ.get(DRIFT_DISABLED_ENV_VAR) == "1":
        _log(
            f"[ctxr-fsm supervisor] drift detector disabled via "
            f"{DRIFT_DISABLED_ENV_VAR}=1; skipping bind."
        )
        return
    # Lazy import to avoid a hard dependency on SQLAlchemy at module
    # import time — keeps ``ctxr-fsm serve --help`` fast when the
    # sqlite extras are not installed.
    from ctxr.fsm.sqlite import Project

    try:
        project = Project.open(db_path, migrate=False)
    except Exception:
        _log(
            "[ctxr-fsm supervisor] drift detector could not open the "
            "project; loop disabled this run."
        )
        return
    try:
        await drift_detector_loop(project, DriftConfig())
    finally:
        project.close()


async def _signal_scope(cancel_scope: anyio.CancelScope) -> None:
    """Translate SIGINT/SIGTERM into a task-group cancellation.

    anyio's portable signal API hides the OS-specific dance (POSIX
    signal handlers vs Windows console events) so this helper stays
    one line of business logic.
    """
    with anyio.open_signal_receiver(signal.SIGINT, signal.SIGTERM) as signals:
        async for _sig in signals:
            _log("[ctxr-fsm supervisor] shutdown signal received; draining...")
            cancel_scope.cancel()
            return


# ---------------------------------------------------------------------------
# Active-MCP discovery file (W14c)
# ---------------------------------------------------------------------------


def _subsystem_payload(
    name: str,
    *,
    project_root: Path,
    port: int | None,
    fallback_healthz: bool,
) -> dict[str, Any] | None:
    """Build the per-subsystem block for ``active-mcp.json`` or None.

    Returns ``None`` when ``port`` is ``None`` (the subsystem was not
    booted in this mode) so the caller can simply drop unset slots.

    Healthz URL convention:

    * MCP: ``http://127.0.0.1:<port>/healthz`` (FastMCP exposes one).
    * API: ``http://127.0.0.1:<port>/healthz`` (FastAPI mount).
    * UI: no /healthz — Vite has none. Field set to ``None`` so a
      consumer that wants to probe falls through to its own waiter
      (the UI's "ready" signal is the dev banner, not a route).

    ``pid`` is read straight from the singleton pid file the supervisor
    just rewrote with the child's pid (see ``_record_child_pid``).
    Missing or malformed pid file leaves the field as ``None``;
    consumers should treat that as "supervisor still booting".
    """
    if port is None:
        return None
    pid_path = pid_file_for(name, project_root=project_root)
    pid_record = read_pid_file(pid_path)
    pid: int | None = None
    if pid_record is not None:
        raw = pid_record.get("pid")
        if isinstance(raw, int):
            pid = raw

    base_url = f"http://127.0.0.1:{port}"
    payload: dict[str, Any] = {
        "http_url": base_url + ("/sse" if name == "mcp" else ""),
        "healthz_url": (
            f"{base_url}/healthz" if (fallback_healthz or name != "ui") else None
        ),
        "pid": pid,
    }
    if name == "api":
        # The API child mounts OpenAPI docs at ``/docs``; surfaced here
        # so a browser-first consumer doesn't have to know the FastAPI
        # default off by heart.
        payload["docs_url"] = f"{base_url}/docs"
    return payload


def _publish_active_mcp_file(
    *,
    project_root: Path,
    ports: dict[str, int | None],
    include_ui: bool,
) -> None:
    """Write the W14c discovery document for the supervisor's current state.

    Schema (per the plan):

    .. code-block:: json

        {
          "started_at": "ISO-8601 UTC ms",
          "supervisor_pid": 1234,
          "version": "0.2.0",
          "subsystems": {
            "mcp": {"http_url": ..., "healthz_url": ..., "pid": 1},
            "api": {"http_url": ..., "healthz_url": ..., "pid": 2,
                     "docs_url": ...},
            "ui":  {"http_url": ..., "healthz_url": null, "pid": 3}
          }
        }

    Subsystems whose port is ``None`` are omitted (``--mcp-only`` runs
    omit api + ui; prod mode omits ui). The MCP entry is always
    present at call time because the publish call is gated on a
    successful MCP healthz; this helper does not re-check.
    """
    # Lazy import to avoid pulling ``ctxr.fsm`` (which transitively
    # imports the entire engine surface) at supervisor module-import
    # time. ``ctxr-fsm --help`` should stay cheap.
    from ctxr.fsm import __version__ as _pkg_version

    subsystems: dict[str, Any] = {}
    mcp_block = _subsystem_payload(
        "mcp", project_root=project_root, port=ports.get("mcp"), fallback_healthz=False
    )
    if mcp_block is not None:
        subsystems["mcp"] = mcp_block
    api_block = _subsystem_payload(
        "api", project_root=project_root, port=ports.get("api"), fallback_healthz=False
    )
    if api_block is not None:
        subsystems["api"] = api_block
    if include_ui:
        ui_block = _subsystem_payload(
            "ui", project_root=project_root, port=ports.get("ui"), fallback_healthz=False
        )
        if ui_block is not None:
            subsystems["ui"] = ui_block

    payload: dict[str, Any] = {
        "started_at": now_iso_ms(),
        "supervisor_pid": os.getpid(),
        "version": _pkg_version,
        "subsystems": subsystems,
    }
    remember_active_mcp_file(payload, project_root=project_root)


def _augment_active_with_status(active: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of the W14c discovery doc with ``status='ready'`` per subsystem.

    The discovery file itself never persists a ``status`` field (the
    file describes "what the supervisor published", not "what the
    last probe said"), but the W14j renderer's colour mapping is keyed
    on ``status``. At supervisor boot time, by definition, every
    subsystem in the file just passed its healthz / liveness check —
    so injecting ``ready`` here produces a green-coloured first-boot
    table without lying about the source-of-truth shape of the file.
    """
    subsystems = active.get("subsystems") or {}
    if not isinstance(subsystems, dict):
        return active
    new_subs: dict[str, Any] = {}
    for name, block in subsystems.items():
        if isinstance(block, dict):
            new_block = dict(block)
            new_block.setdefault("status", "ready")
            new_subs[name] = new_block
        else:
            new_subs[name] = block
    augmented = dict(active)
    augmented["subsystems"] = new_subs
    return augmented


def _publish_active_mcp_file_from_disk(*, project_root: Path) -> None:
    """Re-publish ``active-mcp.json`` after a reload, reading ports from disk.

    The reload loop's contract is "drain + respawn MCP + API, leave UI
    alone". The fresh pids land in the singleton pid files via
    ``_record_child_pid``, and the per-subsystem ports stay the same
    (we ``remember_port`` after every spawn and the new spawn prefers
    the remembered port). So a faithful "what's running right now"
    document just needs to ask :func:`recall_port` for each subsystem
    and let :func:`_subsystem_payload` walk the pid files.

    If the MCP port can't be recalled (something pathological happened
    during the reload), we skip the publish rather than write a
    document with a missing critical key — better to keep the previous
    document live than to lie about the state.
    """
    ports: dict[str, int | None] = {
        "mcp": recall_port("mcp", project_root=project_root),
        "api": recall_port("api", project_root=project_root),
        "ui": recall_port("ui", project_root=project_root),
    }
    if ports["mcp"] is None:
        return
    _publish_active_mcp_file(
        project_root=project_root,
        ports=ports,
        include_ui=ports["ui"] is not None,
    )


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


async def run_supervisor(
    # Literal kept (not an enum) because ``mode`` is a one-off boot
    # parameter consumed by the supervisor + the dev-reload sibling;
    # no closed-vocabulary status field, no cross-module reuse.
    mode: Literal["dev", "prod"] = "dev",  # audit-strings: justified
    db_path: Path | None = None,
    project_root: Path | None = None,
    mcp_only: bool = False,
) -> None:
    """Boot the unified ``ctxr-fsm serve`` supervisor.

    Spawns MCP (HTTP transport), API, and — in ``dev`` mode — the UI
    dev server inside a single :func:`anyio.create_task_group`. In
    ``dev`` mode a sibling task watches ``project_root/ctxr/fsm`` and
    drains+respawns the MCP + API children on every change (debounced
    to 500ms). SIGINT or SIGTERM cancels the task group, which sends
    SIGTERM to every child; children that overrun the drain budget
    are escalated to ``kill()``.

    Reuse semantics: if :func:`acquire_singleton` reports an existing
    healthy instance for any subsystem, the supervisor logs the URL
    and skips its own spawn for that subsystem. The banner still
    advertises the URL so the operator sees the same surface either
    way.
    """
    project_root = (project_root or Path.cwd()).resolve()

    # State the reload loop and shutdown path need to mutate. We use
    # mutable dicts (rather than locals) so the helpers can swap a
    # child in place without re-plumbing every caller.
    children: dict[str, anyio.abc.Process | None] = {"mcp": None, "api": None, "ui": None}
    locks: dict[str, PidLock | None] = {"mcp": None, "api": None, "ui": None}
    ports: dict[str, int | None] = {"mcp": None, "api": None, "ui": None}

    try:
        async with anyio.create_task_group() as tg:
            # Signal handler — must be registered inside the task
            # group so its cancellation propagates to every child.
            tg.start_soon(_signal_scope, tg.cancel_scope)

            # Boot order: API first so the UI child can see the API
            # port the moment Vite starts. MCP last because it has
            # the slowest cold-start (FastMCP import graph). When
            # ``mcp_only`` is set we skip the API entirely (W14b's
            # headless-CI path), leaving only MCP in the subsystem set.
            mcp_healthz_ok = False
            boot_order = ("mcp",) if mcp_only else ("api", "mcp")
            for name in boot_order:
                lock, port, proc, healthz_ok = await _boot_subsystem(
                    name,
                    project_root=project_root,
                    db_path=db_path,
                    task_group=tg,
                )
                children[name] = proc
                locks[name] = lock
                ports[name] = port
                if name == "mcp":
                    mcp_healthz_ok = healthz_ok

            if mode == "dev" and not mcp_only:
                lock, port, proc, _ok = await _boot_subsystem(
                    "ui",
                    project_root=project_root,
                    db_path=db_path,
                    task_group=tg,
                    is_ui=True,
                )
                children["ui"] = proc
                locks["ui"] = lock
                ports["ui"] = port

            # W14c: publish ``.ctxr-fsm/active-mcp.json`` once the MCP
            # child has answered /healthz. Skips the write when MCP
            # never came up — pointing skills at a dead URL would just
            # make their fallback path noisier. Includes whatever
            # subsystems we actually booted (UI omitted in prod mode
            # or mcp_only; API omitted in mcp_only).
            if mcp_healthz_ok and ports["mcp"] is not None:
                # ``include_ui`` MUST reflect what actually got booted,
                # not just the mode flag: the UI-boot path skips when
                # ``ui/`` doesn't exist in the project root (W14k
                # BLOCKER-1) and signals "absent" by leaving
                # ``ports["ui"]`` as None. Recording UI in the
                # discovery file in that case surfaces a dead URL in
                # ``ctxr-fsm urls`` / ``doctor`` — operators ⌘-click +
                # get ECONNREFUSED. The right key is "did this boot
                # pass produce a UI port at all?"
                _publish_active_mcp_file(
                    project_root=project_root,
                    ports=ports,
                    include_ui=(
                        mode == "dev"
                        and not mcp_only
                        and ports["ui"] is not None
                    ),
                )

            # Banner. We render even-when-skipped URLs because the
            # operator's mental model is "here are the three things
            # ctxr-fsm serve gives me" — not "here are the things
            # this particular invocation booted".
            banner_bits = [
                f"mcp=http://localhost:{ports['mcp']}" if ports["mcp"] else "mcp=skipped",
                f"api=http://localhost:{ports['api']}" if ports["api"] else "api=skipped",
            ]
            if mode == "dev":
                banner_bits.append(
                    f"ui=http://localhost:{ports['ui']}" if ports["ui"] else "ui=skipped"
                )
            _log("[ctxr-fsm supervisor] booted: " + ", ".join(banner_bits))

            # W14j: once every required healthz has passed (the publish
            # gate above already enforces ``mcp_healthz_ok``), print the
            # shared subsystem table to stdout exactly once so an
            # operator running ``ctxr-fsm serve`` sees the FastAPI /
            # Swagger / UI / MCP URLs at the moment the dashboard is
            # actually reachable. NOT on every reload — the reload loop
            # below re-publishes ``active-mcp.json`` from disk but does
            # NOT re-print the table; one line per change in the
            # operator's terminal is plenty. NOT on every healthz probe
            # — that would spam the surface.
            if mcp_healthz_ok and ports["mcp"] is not None:
                active = read_active_mcp_file(project_root)
                if active is not None:
                    # ``print_subsystem_table`` writes to a fresh
                    # ``rich.console.Console`` (i.e. stdout). The
                    # supervisor's own log lines go to stderr, so the
                    # operator sees the table on stdout and the
                    # ``[ctxr-fsm supervisor] ...`` chatter on stderr
                    # — two independent streams, easy to pipe
                    # separately for downstream tooling.
                    augmented = _augment_active_with_status(active)
                    print_subsystem_table(augmented, project_root=project_root)

            # Dev-mode reload loop. Sibling task; cancellation of the
            # group tears it down with the rest. anyio's
            # ``start_soon`` is positional-only, so we bind the kwargs
            # via :func:`functools.partial` before handing the coroutine
            # factory to the task group.
            if mode == "dev":
                tg.start_soon(
                    functools.partial(
                        _reload_loop,
                        project_root=project_root,
                        db_path=db_path,
                        task_group=tg,
                        children=children,
                        locks=locks,
                    )
                )

            # W12 drift detector. Started in both ``dev`` and ``prod``
            # modes so the enforcement layer is always live; the only
            # way to disable it is the ``CTXR_FSM_DRIFT_DISABLED=1``
            # env var honoured inside :func:`_drift_detector_task`.
            # We need a database path to bind a Project; when none is
            # supplied (e.g. unit tests that exercise the supervisor
            # against an empty CWD) we log + skip the task rather than
            # crash the boot.
            if db_path is not None:
                tg.start_soon(_drift_detector_task, db_path)
            else:
                _log(
                    "[ctxr-fsm supervisor] no --db path provided; "
                    "drift detector loop disabled."
                )

            # The task group's __aexit__ blocks until every child task
            # returns (or the group is cancelled). The signal scope
            # cancels on SIGINT/SIGTERM; the stdio bridges return when
            # their child exits; the reload loop is cancelled by the
            # signal scope. Nothing further to do here.
    finally:
        # Shutdown: SIGTERM every still-running child, give them the
        # drain budget, then release singletons. We do this in a
        # ``finally`` so the cleanup also runs if the task group
        # propagates an exception (e.g. a child crashed and re-raised
        # via the stdio bridge).
        with anyio.CancelScope(shield=True):
            with anyio.move_on_after(_DRAIN_TIMEOUT_SECONDS + _FINAL_GRACE_SECONDS):
                async with anyio.create_task_group() as shutdown_tg:
                    for name, proc in children.items():
                        if proc is not None:
                            shutdown_tg.start_soon(_drain_child, name, proc)
            for _name, lock in locks.items():
                if lock is not None:
                    release_singleton(lock, project_root=project_root)
            # W14c: drop the discovery file so a later ``ensure`` /
            # skill bootstrap doesn't try to talk to a stopped
            # supervisor. Best-effort: a stray file is annoying, not
            # broken (the healthz probes will fail anyway).
            remove_active_mcp_file(project_root=project_root)
        _log("[ctxr-fsm supervisor] stopped.")


def main(
    mode: str = "dev",
    db_path: Path | None = None,
    mcp_only: bool = False,
) -> None:
    """Synchronous entry point for the Typer CLI.

    Validates ``mode`` against the documented set so we never pass an
    unknown literal into the async body (where the :class:`Literal`
    annotation would be silently lost at runtime). Any other parameter
    is forwarded verbatim. ``mcp_only=True`` makes the supervisor boot
    ONLY the MCP child (W14b's headless-CI / ensure-mcp-only path).
    """
    if mode not in ("dev", "prod"):
        raise ValueError(f"mode must be 'dev' or 'prod' (got {mode!r})")
    # ``anyio.run``'s positional signature is variadic-untyped, so the
    # checker can't narrow ``mode: str`` back to the ``Literal`` the
    # async body declares. We pin it locally with a partial so the
    # call site stays type-safe without sprinkling ``cast`` at the
    # entry point.
    runner: Literal["dev", "prod"] = "prod" if mode == "prod" else "dev"  # audit-strings: justified
    anyio.run(
        functools.partial(run_supervisor, runner, db_path, None, mcp_only)
    )
