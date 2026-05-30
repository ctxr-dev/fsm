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
from typing import Literal

import anyio
import httpx
from watchfiles import awatch

from ctxr.fsm.cli.lifecycle.primitives import (
    PidLock,
    ReusedSubsystem,
    acquire_singleton,
    pick_port,
    pid_file_for,
    recall_port,
    release_singleton,
    remember_port,
    write_pid_file,
)

__all__ = ["main", "run_supervisor"]


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

    The leading ``--`` is npm's pass-through marker — everything after
    it is handed verbatim to the underlying ``vite`` invocation.
    """
    return ["npm", "run", "dev", "--", "--port", str(port)]


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
) -> tuple[PidLock | None, int | None, anyio.abc.Process | None]:
    """Acquire the singleton for ``name`` and (maybe) spawn the child.

    Returns ``(lock, port, proc)`` where any element may be ``None``:

    * ``lock is None`` when :func:`acquire_singleton` returned a
      :class:`ReusedSubsystem` — we skipped spawning entirely.
    * ``proc is None`` likewise indicates "reuse" (no child of ours).
    * ``port`` is the bound port whether we spawned or reused; the
      banner needs both cases.

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
        # to avoid divergence with the live singleton).
        try:
            reused_port = int(acq.probe_url.rsplit(":", 1)[-1].rstrip("/"))
        except (ValueError, AttributeError):
            reused_port = port
        return None, reused_port, None

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
        cmd = _ui_cmd(port=port)
        cwd = project_root / "ui"
        env = os.environ.copy()
        # The Vite config reads VITE_API_PORT at startup to wire the
        # /api/v1 proxy target; passing the actual api port keeps the
        # UI talking to the supervisor's API child.
        api_port = recall_port("api", project_root=project_root)
        if api_port is not None:
            env["VITE_API_PORT"] = str(api_port)
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
    if not is_ui:
        ok = await _poll_healthz(probe_url)
        if not ok:
            _log(
                f"[ctxr-fsm supervisor] {name} did not answer /healthz within "
                f"{_HEALTH_PROBE_BUDGET_SECONDS:.0f}s — continuing anyway."
            )

    # The bind has happened (the child is running); record the port so
    # the next restart prefers it.
    remember_port(name, port, project_root=project_root)

    return acq, port, proc


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
            new_lock, _new_port, new_proc = await _boot_subsystem(
                name,
                project_root=project_root,
                db_path=db_path,
                task_group=task_group,
            )
            children[name] = new_proc
            locks[name] = new_lock

        _log("[ctxr-fsm supervisor] respawned mcp + api")


# ---------------------------------------------------------------------------
# Signal scope
# ---------------------------------------------------------------------------


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
# Public entry points
# ---------------------------------------------------------------------------


async def run_supervisor(
    mode: Literal["dev", "prod"] = "dev",
    db_path: Path | None = None,
    project_root: Path | None = None,
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
            # the slowest cold-start (FastMCP import graph).
            for name in ("api", "mcp"):
                lock, port, proc = await _boot_subsystem(
                    name,
                    project_root=project_root,
                    db_path=db_path,
                    task_group=tg,
                )
                children[name] = proc
                locks[name] = lock
                ports[name] = port

            if mode == "dev":
                lock, port, proc = await _boot_subsystem(
                    "ui",
                    project_root=project_root,
                    db_path=db_path,
                    task_group=tg,
                    is_ui=True,
                )
                children["ui"] = proc
                locks["ui"] = lock
                ports["ui"] = port

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
        _log("[ctxr-fsm supervisor] stopped.")


def main(mode: str = "dev", db_path: Path | None = None) -> None:
    """Synchronous entry point for the Typer CLI.

    Validates ``mode`` against the documented set so we never pass an
    unknown literal into the async body (where the :class:`Literal`
    annotation would be silently lost at runtime). Any other parameter
    is forwarded verbatim.
    """
    if mode not in ("dev", "prod"):
        raise ValueError(f"mode must be 'dev' or 'prod' (got {mode!r})")
    # ``anyio.run``'s positional signature is variadic-untyped, so the
    # checker can't narrow ``mode: str`` back to the ``Literal`` the
    # async body declares. We pin it locally with a partial so the
    # call site stays type-safe without sprinkling ``cast`` at the
    # entry point.
    runner: Literal["dev", "prod"] = "prod" if mode == "prod" else "dev"
    anyio.run(functools.partial(run_supervisor, runner, db_path, None))
