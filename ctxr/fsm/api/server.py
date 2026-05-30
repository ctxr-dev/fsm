"""Entry point for the ``ctxr-fsm api`` HTTP server.

This module owns the *boot sequence* for the API server, mirroring
the structure of :mod:`ctxr.fsm.mcp.server`:

1. Resolve the SQLite database path with the same layered precedence
   the CLI and the MCP server use (``--db`` > ``$CTXR_FSM_DB`` >
   ``./.ctxr-fsm/fsm.db``).
2. Open a :class:`Project` against that path (running migrations
   transparently so a brand-new database boots without operator
   intervention).
3. Bind the open project onto the API package's process-wide handle
   via :func:`ctxr.fsm.api._state.set_project` so every route
   dependency can fetch it.
4. If the caller asked for an ephemeral port (``port=0``), pick a
   free one before handing it to uvicorn so the chosen port is
   loggable / returnable to the caller — uvicorn itself supports
   ``port=0`` but does not expose the chosen value before the server
   starts.
5. Run uvicorn against the FastAPI ``app`` and block until it
   returns (Ctrl-C, SIGTERM, or programmatic shutdown).
6. Close the project in a ``finally`` clause so the SQLite file lock
   is released even if uvicorn exits abnormally.

Why duplicate the ``resolve_db_path`` logic from the CLI and the MCP
server? Same reason as :mod:`ctxr.fsm.mcp.server`: keeping the API
package importable without dragging Typer into memory. The helper is
small, exhaustively commented, and trivially kept in sync.
"""

from __future__ import annotations

import logging
import os
import socket
from pathlib import Path

import uvicorn

from ctxr.fsm.api import _state, app
from ctxr.fsm.sqlite import Project

__all__ = ["main", "pick_free_port", "resolve_db_path"]


# Logger for the boot sequence itself. Per-route logging happens
# inside the route modules (or via uvicorn's access log); keeping
# this logger separate makes ``ctxr.fsm.api.server`` easy to grep
# for startup / shutdown events without the per-request chatter.
_LOG = logging.getLogger("ctxr.fsm.api.server")


# Mirrors :data:`ctxr.fsm.cli._common._DEFAULT_DB_RELATIVE` and the
# constant of the same name in :mod:`ctxr.fsm.mcp.server`. Duplicated
# rather than imported so the API package does not transitively pull
# in either Typer (CLI) or FastMCP (MCP) — see the module docstring.
_DEFAULT_DB_RELATIVE: Path = Path(".ctxr-fsm") / "fsm.db"
_DB_ENV_VAR: str = "CTXR_FSM_DB"


def resolve_db_path(db_opt: Path | None) -> Path:
    """Apply the project's layered precedence to produce a concrete DB path.

    Precedence (highest first):

    1. The explicit ``db_opt`` argument (``--db``).
    2. ``$CTXR_FSM_DB`` from the process environment.
    3. ``./.ctxr-fsm/fsm.db`` relative to the current working
       directory.

    Identical contract to :func:`ctxr.fsm.cli._common.resolve_db_path`
    and :func:`ctxr.fsm.mcp.server.resolve_db_path`; kept independent
    so the API package does not transitively depend on either of
    those.
    """
    if db_opt is not None:
        return db_opt.expanduser().resolve()
    env_value = os.environ.get(_DB_ENV_VAR)
    if env_value:
        return Path(env_value).expanduser().resolve()
    return (Path.cwd() / _DEFAULT_DB_RELATIVE).resolve()


def pick_free_port(host: str) -> int:
    """Bind a transient socket to ``(host, 0)`` and return the assigned port.

    Used when the caller passes ``port=0`` to :func:`main` — we let
    the kernel pick a free ephemeral port, then immediately close the
    socket and hand the number to uvicorn. There is a tiny TOCTOU
    window where another process could grab the same port before
    uvicorn re-binds; in practice this is harmless for the dev /
    test scenarios that use ``port=0`` (uvicorn will simply fail to
    bind and the caller will see the error). Production deployments
    should always pass an explicit port.

    Why pre-pick the port instead of just passing ``port=0`` to
    uvicorn? Because uvicorn does not expose the chosen port before
    the server is running, which makes ``port=0`` useless for tests
    that need to point a client at the API. Pre-picking gives the
    caller the number up front so they can log it / connect to it.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        # ``SO_REUSEADDR`` doesn't help here (we close the socket
        # before uvicorn binds, and Linux's ephemeral-port pool will
        # generally not hand back the same port within the TIME_WAIT
        # window anyway), but setting it is harmless and matches the
        # convention uvicorn itself uses.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def main(
    host: str = "127.0.0.1",
    port: int = 0,
    reload: bool = False,
    db_path: Path | None = None,
) -> None:
    """Boot the API server and block until uvicorn returns.

    Parameters
    ----------
    host:
        Interface to bind. Defaults to loopback for safety — the
        API has no transport encryption and only the loopback
        binding is appropriate without an upstream TLS terminator.
        Operators who want remote access should pass ``0.0.0.0``
        explicitly and put a reverse proxy in front.
    port:
        TCP port. Pass ``0`` to let the OS pick a free port (useful
        for tests and developer-loop scripts); production callers
        should pass an explicit number.
    reload:
        Forwarded to uvicorn. Enables auto-reload on source changes
        — only useful when developing the API itself. Reload mode
        runs uvicorn in a watchdog parent that re-execs the worker,
        which means we cannot pre-open the :class:`Project` (the
        re-execed worker would not see the binding). The reload
        path therefore relies on the lifespan handler to open its
        own project; the non-reload path opens the project here so
        ``ctxr-fsm api`` startup logs include the resolved DB path
        before uvicorn starts.
    db_path:
        Optional explicit database path; if ``None``, falls back to
        :func:`resolve_db_path` (env-var, then project default).
    """
    resolved_db = resolve_db_path(db_path)

    # When the caller asked for an ephemeral port, pick it now so we
    # can log it and so test harnesses calling ``main`` from a
    # subprocess can read it back from stdout. uvicorn's own
    # ``port=0`` support would otherwise hide the chosen value until
    # after the server is running.
    if port == 0:
        port = pick_free_port(host)

    if reload:
        # Reload mode re-execs the worker on every file change, which
        # means any project we opened here would be invisible to the
        # re-execed worker. Let the lifespan handler open its own
        # project against the same resolved path by exporting it via
        # the env-var (this is the same precedence the lifespan
        # already honours, so no special-casing is needed inside the
        # app — we just make sure the env-var is set).
        os.environ[_DB_ENV_VAR] = str(resolved_db)
        _LOG.info(
            "starting API server on http://%s:%s (reload, DB=%s)",
            host,
            port,
            resolved_db,
        )
        # In reload mode uvicorn requires the app reference as an
        # import string so the worker process can re-import it after
        # each reload. The non-reload path passes the live ``app``
        # object below.
        uvicorn.run(
            "ctxr.fsm.api:app",
            host=host,
            port=port,
            reload=True,
        )
        return

    _LOG.info("opening ctxr.fsm project at %s", resolved_db)
    # ``migrate=True`` upgrades a stale schema in place so a brand-new
    # database boots without an operator running ``ctxr-fsm migrate``
    # first. The migration is idempotent.
    project = Project.open(resolved_db, migrate=True)
    _state.set_project(project)

    _LOG.info("starting API server on http://%s:%s", host, port)
    try:
        uvicorn.run(app, host=host, port=port)
    finally:
        # Covers both the normal exit path (uvicorn returns after
        # SIGINT / programmatic shutdown) and the error path (uvicorn
        # raised). The lifespan handler will not close the project
        # because the entry-point owns it — that contract is encoded
        # in :func:`ctxr.fsm.api.lifespan_handler`.
        _LOG.info("uvicorn returned, closing project")
        try:
            project.close()
        finally:
            _state.reset_project()
