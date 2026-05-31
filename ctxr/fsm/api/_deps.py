"""FastAPI dependencies shared across every router.

Two dependencies live here:

* :func:`get_project` — yields the process-wide :class:`Project`
  handle that the lifespan hook bound at startup. Routes that need to
  touch SQLite take ``project: Project = Depends(get_project)`` and
  receive the same handle every request.
* :func:`require_auth` — calls into :mod:`ctxr.fsm.api._auth` to
  validate the ``Authorization`` header. Routes that should be
  guarded add ``_: None = Depends(require_auth)`` (or the router can
  apply it once via ``dependencies=[Depends(require_auth)]``).

Keeping these in their own module — separate from the FastAPI app
construction in :mod:`ctxr.fsm.api` — avoids the circular import that
would otherwise occur when individual route modules need both
``Depends(get_project)`` and ``Depends(require_auth)`` while the app
module imports those routers.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header

from ctxr.fsm.api import _auth, _state
from ctxr.fsm.sqlite import Project

__all__ = [
    "ProjectDep",
    "get_project",
    "require_auth",
]


def get_project() -> Project:
    """Return the bound :class:`Project`, raising if the server is not up.

    Thin wrapper around :func:`ctxr.fsm.api._state.get_project` so
    routes don't need to know about the ``_state`` module — they take
    ``Depends(get_project)`` and remain decoupled from the binding
    mechanism, which lets tests swap in alternate dependencies via
    FastAPI's ``app.dependency_overrides`` without touching the
    underlying global.
    """
    return _state.get_project()


def require_auth(
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> None:
    """FastAPI dependency wrapper around :func:`_auth.check_authorization`.

    Declared as a plain dependency (not middleware) so the
    ``Authorization`` header shows up in the OpenAPI schema for
    routes that opt in, and so individual routes can choose to skip
    auth (health probes, the OpenAPI docs themselves) by simply not
    declaring the dependency.
    """
    _auth.check_authorization(authorization)


# Convenience type alias so route modules can write
# ``project: ProjectDep`` instead of repeating the verbose
# ``Annotated[Project, Depends(get_project)]`` each time. Keeps route
# signatures readable while preserving the FastAPI dependency wiring.
ProjectDep = Annotated[Project, Depends(get_project)]
