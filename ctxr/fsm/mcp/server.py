"""Entry point for the ``ctxr-fsm mcp`` server.

This module owns the *boot sequence* for the MCP server:

1. Resolve the SQLite database path using the same layered precedence
   the CLI uses (``--db`` > ``$CTXR_FSM_DB`` > ``./.ctxr-fsm/fsm.db``).
2. Open a :class:`Project` against that path (running migrations
   transparently so a brand-new database boots without operator
   intervention).
3. Bind the open project onto the process-wide handle via
   :func:`set_project` so every tool body can fetch it.
4. Install a stderr-only logging configuration — MCP stdio uses
   **stdout** for the JSON-RPC framing, so any print/log that escapes
   to stdout corrupts the protocol stream.
5. Install signal handlers so SIGINT / SIGTERM close the project
   cleanly (releasing the SQLite file lock) instead of leaking the
   engine.
6. Hand control to FastMCP's transport loop — stdio (the default,
   client-launched) or HTTP-SSE.

Why duplicate ``resolve_db_path`` logic from the CLI? We want
``ctxr.fsm.mcp`` to be importable without pulling Typer into memory
(the CLI module imports ``typer`` at top level, which would force a
hard ``typer`` dependency on every MCP consumer). The duplicated
helper is small, well-commented, and trivially kept in sync with the
canonical CLI version.

Graceful drain on SIGTERM
-------------------------

The W7 service-lifecycle wave wires the supervisor: when a source
file changes the supervisor sends **SIGTERM** to the MCP child, which
is the polite "drain and stop" signal. The child's SIGTERM handler:

1. Flips the drain flag via :func:`ctxr.fsm.mcp._drain.start_drain`
   so every subsequent tool call is rejected with a structured
   ``server_draining`` error envelope (the :func:`drain_aware`
   decorator wrapped onto every ``fsm.*`` tool body enforces this).
2. Blocks on :func:`ctxr.fsm.mcp._drain.wait_for_drain` until every
   in-flight tool call has decremented the counter, or until the
   configured drain timeout elapses (default 30 s).
3. Closes the :class:`Project` cleanly (releasing the SQLite file
   lock) and exits with status 0.

**SIGINT** keeps its original immediate-exit semantics — Ctrl-C from
an operator means "stop now" and the operator already expects any
in-flight work to be lost.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from pathlib import Path
from types import FrameType
from typing import Literal

from ctxr.fsm.mcp import mcp
from ctxr.fsm.mcp._drain import (
    DEFAULT_DRAIN_TIMEOUT_SECONDS,
    start_drain,
    wait_for_drain,
)
from ctxr.fsm.mcp._state import reset_project, set_project
from ctxr.fsm.sqlite import Project

__all__ = ["main", "resolve_db_path"]


# Logger used by the boot sequence itself. Per-tool logging happens
# inside the tool modules — keeping the entry-point logger separate
# makes it easy to grep "mcp.server" in the journal for startup /
# shutdown events without the per-call chatter.
_LOG = logging.getLogger("ctxr.fsm.mcp.server")


# Mirrors :data:`ctxr.fsm.cli._common._DEFAULT_DB_RELATIVE`. Duplicated
# (rather than imported) so the MCP server does not transitively depend
# on the Typer-heavy CLI module — see the module docstring.
_DEFAULT_DB_RELATIVE: Path = Path(".ctxr-fsm") / "fsm.db"
_DB_ENV_VAR: str = "CTXR_FSM_DB"


def _walk_up_for_state_dir(start: Path) -> Path | None:
    """Return the nearest ancestor of ``start`` that contains ``.ctxr-fsm``.

    Walk-up makes the stdio MCP entry portable across projects: the
    client (Claude Code / Cursor / Codex) spawns ``ctxr-fsm mcp`` with
    the user's CURRENT cwd, and we walk up to find the project root
    even when the user is several directories deep. Returns ``None``
    when no ancestor contains the marker, so the caller can fall back
    to the cwd default (and surface a helpful "did you run init?"
    error if the file is missing too).
    """
    current = start.resolve()
    for candidate in [current, *current.parents]:
        if (candidate / ".ctxr-fsm").is_dir():
            return candidate
    return None


def resolve_db_path(db_opt: Path | None) -> Path:
    """Apply the project's layered precedence to produce a concrete DB path.

    Precedence (highest first):

    1. The explicit ``db_opt`` argument (e.g. ``--db``).
    2. ``$CTXR_FSM_DB`` from the process environment.
    3. Walk up from the current working directory looking for an
       ancestor that contains ``.ctxr-fsm/``; return that ancestor's
       ``.ctxr-fsm/fsm.db``.
    4. Fall back to ``./.ctxr-fsm/fsm.db`` relative to the current
       working directory. This may not exist yet; the caller surfaces
       a helpful "did you run ``ctxr-fsm init``?" error.

    The walk-up tier (step 3) is what makes the stdio MCP entry
    portable across projects (W14d). A client (Claude Code, Cursor,
    Codex) launches ``ctxr-fsm mcp`` with the user's CURRENT cwd
    inherited; the walk-up finds the right project root even when the
    user is several directories deep.

    Identical contract to :func:`ctxr.fsm.cli._common.resolve_db_path`
    (which also gained the walk-up tier); kept independent so the MCP
    package does not transitively depend on the Typer-based CLI
    module.
    """
    if db_opt is not None:
        return db_opt.expanduser().resolve()
    env_value = os.environ.get(_DB_ENV_VAR)
    if env_value:
        return Path(env_value).expanduser().resolve()
    found_root = _walk_up_for_state_dir(Path.cwd())
    if found_root is not None:
        return (found_root / _DEFAULT_DB_RELATIVE).resolve()
    return (Path.cwd() / _DEFAULT_DB_RELATIVE).resolve()


def _configure_stderr_logging() -> None:
    """Install a logging configuration that writes only to stderr.

    The MCP stdio transport uses **stdout** for JSON-RPC framing — a
    stray ``print()`` or default ``logging.StreamHandler`` (which
    defaults to stderr in modern Python, but historically defaulted to
    stdout under some configurations) corrupts the protocol stream and
    the client disconnects with a parse error. We pin the handler to
    ``sys.stderr`` explicitly so no future Python version change can
    flip the default underneath us.

    ``force=True`` lets us reconfigure even if some imported module
    already called ``logging.basicConfig`` — a common gotcha in
    embedded scenarios.
    """
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )


def _install_signal_handlers(project: Project) -> None:
    """Hook SIGINT / SIGTERM with split-personality semantics.

    The two signals carry *different* operator intent and we honour
    that difference:

    * **SIGINT** — typically Ctrl-C from the controlling terminal. The
      operator wants to stop *now*; any in-flight tool call is
      expected collateral and we do not attempt to drain. We close
      the project (releasing the SQLite file lock) and exit
      immediately.
    * **SIGTERM** — sent by the supervisor when a source change
      triggers a graceful reload, or by a container runtime during a
      polite shutdown. The W7 contract says we must **drain** before
      exiting: flip the drain flag (so new tool calls are refused
      with ``server_draining``), wait for in-flight calls to complete
      (bounded by :data:`DEFAULT_DRAIN_TIMEOUT_SECONDS`), then close
      the project and exit. The drain banners are emitted on stderr
      by :mod:`ctxr.fsm.mcp._drain` so the supervisor's log-tail can
      attribute the lifecycle.

    Both paths exit with status 0 — receiving either signal is a
    normal lifecycle event, not an error.

    On Windows ``SIGTERM`` does not exist; the registration is
    wrapped in a try/except so an import on Windows does not blow up
    — the SIGINT registration is enough on that platform (Windows
    container orchestrators use a different shutdown signalling path
    that we intentionally do not intercept).
    """

    def _shutdown_immediate(signum: int, _frame: FrameType | None) -> None:
        """SIGINT handler: stop now, no drain.

        Used for SIGINT only. The operator pressed Ctrl-C; any
        in-flight tool call is acceptable collateral. We close the
        project to release the SQLite file lock and exit straight
        away. Worth contrasting with :func:`_shutdown_draining`:
        identical postlude, no drain pivot.
        """
        _LOG.info(
            "received SIGINT (signal %s), closing project and exiting immediately",
            signum,
        )
        try:
            project.close()
        finally:
            reset_project()
        raise SystemExit(0)

    # Guard so a duplicate SIGTERM (operators retrying a "stuck"
    # shutdown) does not spawn a second drain thread. The flag is
    # checked + set atomically under the GIL — sufficient for a
    # signal-handler-only writer / reader pair.
    _drain_started: dict[str, bool] = {"flag": False}

    def _drain_thread_target() -> None:
        """Body of the background thread that owns the drain wait.

        Running the drain on a *separate* thread is what makes the
        contract work for async tool bodies: the asyncio event loop
        on the main thread needs to keep spinning so the in-flight
        ``await`` can complete and the per-call ``drain_aware``
        decrement can fire. A drain wait that ran on the main thread
        (inside the signal handler itself) would block the loop and
        deadlock against the very call we are waiting to finish.

        After :func:`wait_for_drain` returns (quiescent or
        budget-exhausted), we close the project to release the
        SQLite file lock and then ``os._exit(0)`` — ``_exit`` is the
        right hammer here because ``sys.exit`` would raise
        :class:`SystemExit` on *this* thread, which the asyncio loop
        on the main thread would never see, and the process would
        keep running. ``os._exit`` terminates the process from
        whatever thread is calling it.
        """
        try:
            wait_for_drain()
        finally:
            try:
                project.close()
            except Exception:  # pragma: no cover - best effort
                _LOG.exception("project.close() raised during drain shutdown")
            try:
                reset_project()
            except Exception:  # pragma: no cover - best effort
                _LOG.exception("reset_project() raised during drain shutdown")
        # Exit code 0 — SIGTERM-driven shutdown is a normal lifecycle
        # event (supervisor reload, container stop). os._exit (not
        # sys.exit) so the termination is visible to the main thread
        # without needing it to observe a SystemExit raised here.
        os._exit(0)

    def _shutdown_draining(signum: int, _frame: FrameType | None) -> None:
        """SIGTERM handler: flip the drain flag, spawn the drain thread.

        The handler itself is intentionally tiny: it flips the drain
        flag (so :func:`drain_aware`-wrapped tools immediately start
        refusing new work) and hands the actual *wait* off to a
        daemon thread. That split is what lets the asyncio loop on
        the main thread keep running so in-flight tool bodies can
        finish and decrement the counter — a wait done on the main
        thread would block the loop and deadlock against the very
        call we are draining.

        Idempotent on repeated SIGTERMs (operators occasionally send
        a second one when the first looks slow) — we only spawn the
        drain thread once; subsequent signals just refresh the
        drain-start log line.
        """
        _LOG.info(
            "received SIGTERM (signal %s); beginning graceful drain", signum
        )
        # ``start_drain`` is itself idempotent (preserves the first
        # timestamp, refreshes the timeout); we still gate the thread
        # spawn to keep at most one drain worker alive.
        start_drain(DEFAULT_DRAIN_TIMEOUT_SECONDS)
        if _drain_started["flag"]:
            return
        _drain_started["flag"] = True
        thread = threading.Thread(
            target=_drain_thread_target,
            name="ctxr-fsm-mcp-drain",
            daemon=True,
        )
        thread.start()

    signal.signal(signal.SIGINT, _shutdown_immediate)
    try:
        signal.signal(signal.SIGTERM, _shutdown_draining)
    except (AttributeError, ValueError):
        # SIGTERM is not available on Windows (AttributeError) or when
        # we're not running in the main thread (ValueError). Both are
        # acceptable degradations: SIGINT alone covers the interactive
        # case, and container orchestrators on Windows have their own
        # shutdown signalling we don't intercept.
        _LOG.debug("SIGTERM handler not installed (unsupported on this platform)")


def main(
    transport: Literal["stdio", "http"] = "stdio",
    db_path: Path | None = None,
    host: str = "127.0.0.1",
    port: int = 0,
) -> None:
    """Boot the MCP server and block until the transport returns.

    Parameters
    ----------
    transport:
        ``"stdio"`` (default, the canonical MCP client-launched
        framing) or ``"http"`` for the SSE transport. The HTTP path is
        an operator escape hatch — Claude Code itself always uses
        stdio.
    db_path:
        Optional explicit database path; if ``None``, falls back to
        :func:`resolve_db_path` (env-var, then project default).
    host, port:
        Listen address for the HTTP transport. Ignored for ``stdio``.
        Default ``port=0`` lets the OS pick an ephemeral port, which
        is mostly useful for tests and developer-loop scripts; production
        deployments should pass an explicit port.
    """
    _configure_stderr_logging()

    resolved_db = resolve_db_path(db_path)
    _LOG.info("opening ctxr.fsm project at %s", resolved_db)

    # ``migrate=True`` upgrades a stale schema in place so a brand-new
    # database boots without an operator running ``ctxr-fsm migrate``
    # first. The migration is idempotent.
    project = Project.open(resolved_db, migrate=True)
    set_project(project)

    _install_signal_handlers(project)

    try:
        if transport == "stdio":
            _LOG.info("starting MCP server on stdio")
            mcp.run()
        elif transport == "http":
            # FastMCP's SSE transport is configured via constructor
            # kwargs (host / port) and selected by passing ``"sse"``
            # to ``run()``. We expose ``"http"`` to the caller as the
            # friendlier name and translate here. The host/port are
            # applied via the underlying settings — FastMCP reads them
            # from ``self.settings.host`` and ``self.settings.port``
            # which we set explicitly so a per-call host/port wins
            # over whatever the constructor default was.
            mcp.settings.host = host
            mcp.settings.port = port
            _LOG.info("starting MCP server on http://%s:%s (sse)", host, port)
            mcp.run(transport="sse")
        else:
            # Defensive — Literal narrows at the type level but a
            # runtime caller (e.g. a typo from the CLI shim) could
            # still squeeze through.
            raise ValueError(
                f"unknown transport {transport!r}; expected 'stdio' or 'http'"
            )
    finally:
        # Cover the path where ``mcp.run`` returns normally (e.g.
        # client closed the stdio pipe) — the signal handlers cover
        # the SIGINT/SIGTERM path.
        _LOG.info("MCP transport returned, closing project")
        try:
            project.close()
        finally:
            reset_project()
