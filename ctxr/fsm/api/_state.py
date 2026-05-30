"""Process-wide :class:`Project` handle for the HTTP API server.

Mirrors :mod:`ctxr.fsm.mcp._state` but lives in its own module so the
API package does not pull the MCP package (and its FastMCP dependency)
into memory. The lifespan handler in :mod:`ctxr.fsm.api` binds the
project here at startup and clears it at shutdown; the FastAPI
dependency :func:`ctxr.fsm.api._deps.get_project` is the single read
path every route uses.

Why a module-level global rather than ``app.state``? FastAPI's
``app.state`` is convenient but ties every helper to an ``app``
parameter; routes can already obtain the app via the ``Request`` but
the dependency layer reads cleanest when ``get_project`` is a plain
zero-arg callable. A module-global keeps the dependency tiny and lets
non-route code (background tasks, the SSE pump) reach for the project
without threading the app through.

The bound project is shared across every request. SQLite connections
are pooled by the underlying :class:`Project` (W2) so concurrent
requests are safe; this module owns only the *binding*, not the
connection lifecycle.
"""

from __future__ import annotations

from ctxr.fsm.sqlite import Project

__all__ = [
    "get_project",
    "is_open",
    "reset_project",
    "set_project",
]


# The active :class:`Project` handle, or ``None`` when the server is
# not currently in its running window (before the lifespan startup
# hook fires, or after its shutdown hook has run). Reads go through
# :func:`get_project`, which raises rather than returning ``None`` so
# misuse surfaces immediately instead of producing an opaque
# ``NoneType has no attribute`` later in the request stack.
_project: Project | None = None


def set_project(project: Project) -> None:
    """Bind ``project`` as the singleton handle for every request.

    Called exactly once from the FastAPI lifespan hook after
    :meth:`Project.open` succeeds. Re-binding is permitted — tests
    swap in an in-memory project mid-run — but the caller is
    responsible for closing the previous project. This helper owns
    only the binding, not the lifecycle.
    """
    global _project
    _project = project


def get_project() -> Project:
    """Return the bound project, raising if the server is not booted.

    The raise-loud behaviour is deliberate: every route uses this via
    the :func:`ctxr.fsm.api._deps.get_project` FastAPI dependency, so
    any call that arrives before the lifespan hook ran (or after it
    tore down) is a programmer error, not a recoverable condition.
    """
    if _project is None:
        raise RuntimeError(
            "ctxr.fsm.api project handle is not set — the FastAPI "
            "lifespan handler must run before requests are served."
        )
    return _project


def reset_project() -> None:
    """Clear the bound project handle (called from lifespan shutdown).

    Does not call :meth:`Project.close` — the caller that opened the
    project owns its lifecycle. This helper only resets the binding so
    subsequent :func:`get_project` calls raise as if the server had
    never booted.
    """
    global _project
    _project = None


def is_open() -> bool:
    """Return ``True`` when a project is currently bound.

    Used by ``GET /readyz`` to report readiness without raising. Tests
    also use this to assert the lifespan hook fired correctly.
    """
    return _project is not None
