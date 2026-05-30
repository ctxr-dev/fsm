"""Process-wide singletons for the MCP server.

The :class:`~mcp.server.fastmcp.FastMCP` instance lives at module scope
in :mod:`ctxr.fsm.mcp` so the ``@mcp.tool()`` decorators in tool
modules can find it at import time. Tools, however, also need access
to the open :class:`~ctxr.fsm.sqlite.Project` handle, which is *not*
known until the server boots and opens the database file.

This module is the bridge: :func:`set_project` is called once by
:func:`ctxr.fsm.mcp.server.main` after the project is opened, and
every tool body calls :func:`get_project` to obtain it. Tests reach
for :func:`reset_project` between cases to ensure isolation.

Why module-level state instead of a context-var or a per-request
dependency? FastMCP's stdio transport is single-process and tools are
plain functions — there is no request context to pin a context-var to,
and there is exactly one ``Project`` per server lifetime. A simple
module-global is the smallest thing that works.
"""

from __future__ import annotations

from ctxr.fsm.sqlite import Project

__all__ = [
    "get_project",
    "reset_project",
    "set_project",
]


# The active Project handle. Initialised to ``None`` so calls before
# the server is configured raise loudly via :func:`get_project` rather
# than silently operating on a stale handle.
_project: Project | None = None


def set_project(project: Project) -> None:
    """Bind ``project`` as the singleton handle every tool will use.

    Called exactly once by the MCP server entry point after
    :meth:`Project.open` succeeds. Re-binding is permitted (tests use
    this to swap in an in-memory project) but the caller is responsible
    for closing the previous project — this helper does not own the
    lifecycle of either value.
    """
    global _project
    _project = project


def get_project() -> Project:
    """Return the bound :class:`Project`, raising if none is bound.

    Raises :class:`RuntimeError` instead of returning ``None`` so that
    every tool body can assume a live project without needing a guard
    clause; any misuse (calling a tool before ``set_project`` runs) is
    surfaced immediately with a precise message rather than turning
    into a ``NoneType has no attribute`` further down the stack.
    """
    if _project is None:
        raise RuntimeError(
            "ctxr.fsm.mcp project handle is not set — "
            "call set_project(Project.open(...)) before invoking tools."
        )
    return _project


def reset_project() -> None:
    """Clear the bound project handle (intended for tests).

    Does not call :meth:`Project.close` because the test fixture that
    set the project is responsible for tearing it down; this helper
    only resets the binding so subsequent calls to :func:`get_project`
    raise as if the server had never been configured.
    """
    global _project
    _project = None
