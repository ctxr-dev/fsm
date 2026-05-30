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
"""

from __future__ import annotations

import logging
import os
import signal
import sys
from pathlib import Path
from types import FrameType
from typing import Literal

from ctxr.fsm.mcp import mcp
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


def resolve_db_path(db_opt: Path | None) -> Path:
    """Apply the project's layered precedence to produce a concrete DB path.

    Precedence (highest first):

    1. The explicit ``db_opt`` argument (e.g. ``--db``).
    2. ``$CTXR_FSM_DB`` from the process environment.
    3. ``./.ctxr-fsm/fsm.db`` relative to the current working directory.

    Identical contract to :func:`ctxr.fsm.cli._common.resolve_db_path`;
    kept independent so the MCP package does not transitively depend on
    the Typer-based CLI module.
    """
    if db_opt is not None:
        return db_opt.expanduser().resolve()
    env_value = os.environ.get(_DB_ENV_VAR)
    if env_value:
        return Path(env_value).expanduser().resolve()
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
    """Hook SIGINT / SIGTERM to close ``project`` before exit.

    FastMCP's stdio loop already responds to SIGINT by stopping the
    event loop, but it does not know about our :class:`Project` so we
    add a tiny shim that closes the engine (releasing the SQLite file
    lock) and clears the global handle before re-raising the default
    behaviour by raising :class:`SystemExit`.

    ``SIGTERM`` is handled symmetrically so containerised deployments
    that send SIGTERM during graceful shutdown also close the engine
    cleanly.

    On Windows ``SIGTERM`` does not exist; the registration is wrapped
    in a try/except so an import on Windows does not blow up — the
    SIGINT registration is enough on that platform.
    """

    def _shutdown(signum: int, _frame: FrameType | None) -> None:
        _LOG.info("received signal %s, closing project and exiting", signum)
        try:
            project.close()
        finally:
            reset_project()
        # Use exit code 0 — receiving SIGTERM is a normal lifecycle
        # event, not an error. The MCP client will reconnect / restart
        # us as it sees fit.
        raise SystemExit(0)

    signal.signal(signal.SIGINT, _shutdown)
    try:
        signal.signal(signal.SIGTERM, _shutdown)
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
