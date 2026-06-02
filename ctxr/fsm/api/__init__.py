"""``ctxr.fsm.api`` — HTTP/SSE surface for the FSM substrate.

This package is the W5 wave: a FastAPI-backed HTTP API that mirrors
the MCP tool surface (W4) for clients that prefer REST/SSE — the
local UI dev server, browser-driven dashboards, third-party
orchestrators that don't speak MCP.

Module layout
-------------

* :mod:`ctxr.fsm.api` (this module) — owns the FastAPI ``app``
  instance, the lifespan handler that opens/closes the SQLite
  :class:`Project`, the CORS + auth middleware wiring, and the small
  set of always-on endpoints (health checks, project metadata, OpenAPI
  at ``/docs``).
* :mod:`ctxr.fsm.api._state` — process-wide :class:`Project` handle
  bound by the lifespan hook.
* :mod:`ctxr.fsm.api._deps` — FastAPI dependencies (``get_project``,
  ``require_auth``) shared across every router.
* :mod:`ctxr.fsm.api._auth` — bearer-token validation logic, factored
  out of the dependency wrapper so tests can exercise the predicate
  directly.
* :mod:`ctxr.fsm.api.server` — entry-point (``main``) that resolves
  the DB path, opens the project, and runs uvicorn. The CLI's
  ``ctxr-fsm api`` subcommand will thunk here in the next phase.

What this layer is NOT
----------------------

* It is not the MCP server. No ``mcp`` SDK imports happen in this
  package; the API is a plain ASGI app sharing the same SQLite
  substrate. Both can run side-by-side against the same database file
  (SQLite's WAL journal handles concurrent readers).
* It is not the UI. The UI lives in ``fsm/ui/`` (W6); this package
  serves the HTTP that the UI consumes.

CORS
----

The CORS allowlist starts with the Vite dev server defaults
(``http://localhost:5173`` and the loopback equivalent) and extends
with anything in ``$CTXR_FSM_API_CORS_ORIGINS`` (comma-separated).
Wildcards are deliberately *not* supported — operators who need
permissive CORS should list every origin explicitly so the audit log
records intent.

Auth
----

When ``CTXR_FSM_API_TOKEN`` is unset, the API is in dev mode and
trusts every request. When it is set, every request (except health
probes and the OpenAPI docs) must carry
``Authorization: Bearer <token>``. See :mod:`ctxr.fsm.api._auth` for
the exact predicate.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import ctxr.fsm
from ctxr.fsm.api import _state
from ctxr.fsm.api._deps import ProjectDep, require_auth
from ctxr.fsm.api._paths import looks_like_filesystem_db_path, project_root_and_relative
from ctxr.fsm.cli.lifecycle.primitives import read_active_mcp_file
from ctxr.fsm.sqlite import Project

__all__ = ["ProjectMetadata", "app", "lifespan_handler"]


# ── CORS allowlist construction ────────────────────────────────────
# Default to the Vite dev server (and the loopback equivalent so
# clients that resolve ``localhost`` to ``127.0.0.1`` still match).
# Operators extend the list via ``$CTXR_FSM_API_CORS_ORIGINS`` as a
# comma-separated string — wildcards are not honoured (see module
# docstring).
_DEFAULT_CORS_ORIGINS: tuple[str, ...] = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
)
_CORS_ENV_VAR: str = "CTXR_FSM_API_CORS_ORIGINS"


def _resolve_cors_origins() -> list[str]:
    """Return the combined CORS allowlist for the running process.

    The env-var extension is parsed once at app construction time;
    operators who need to change it must restart the server. Trimming
    whitespace and dropping empty entries keeps ``a, , b`` from
    accidentally producing an empty-string origin (which would match
    every request with a missing ``Origin`` header — almost certainly
    not what the operator intended).
    """
    extra = os.environ.get(_CORS_ENV_VAR, "")
    parsed = [o.strip() for o in extra.split(",") if o.strip()]
    # ``dict.fromkeys`` preserves insertion order while de-duplicating
    # — important so the env-var origins appear after the defaults in
    # the OpenAPI docs without producing duplicate entries when an
    # operator re-lists one of the defaults explicitly.
    return list(dict.fromkeys([*_DEFAULT_CORS_ORIGINS, *parsed]))


# ── Lifespan handler ───────────────────────────────────────────────
# FastAPI's lifespan is the modern replacement for the deprecated
# ``startup`` / ``shutdown`` event hooks. We use it to open the
# :class:`Project` (if one isn't already bound — the test harness and
# the ``ctxr-fsm api`` entry-point both bind the project *before*
# uvicorn starts, in which case we leave it alone) and to tear it
# down at shutdown.


@asynccontextmanager
async def lifespan_handler(_app: FastAPI) -> AsyncIterator[None]:
    """Open the :class:`Project` for the duration of the app's lifetime.

    Behaviour split:

    * If a project is already bound (the canonical path — the entry
      point in :mod:`ctxr.fsm.api.server` opens the DB and calls
      :func:`_state.set_project` *before* invoking uvicorn so we boot
      with a known-good handle), the lifespan does nothing on entry
      and does nothing on exit. The caller owns the lifecycle.
    * If no project is bound (e.g. ``uvicorn ctxr.fsm.api:app`` is
      run directly, without going through :func:`server.main`), the
      lifespan opens one against the resolved default path, binds it,
      and closes it on shutdown. This keeps ``uvicorn ctxr.fsm.api:app``
      a valid one-liner for ad-hoc local use without forcing every
      caller through the entry-point function.
    """
    opened_here = False
    if not _state.is_open():
        # Local import to avoid the import cycle that would form if the
        # server module imported this package at module scope (it
        # imports ``app`` from here).
        from ctxr.fsm.api.server import resolve_db_path

        db_path = resolve_db_path(None)
        project = Project.open(db_path, migrate=True)
        _state.set_project(project)
        opened_here = True

    try:
        yield
    finally:
        if opened_here:
            # We only close the project we opened ourselves. Projects
            # that arrived pre-bound belong to the caller — typically
            # the ``server.main`` entry-point, which closes them in
            # its own ``finally`` clause after uvicorn returns.
            project = _state.get_project()
            try:
                project.close()
            finally:
                _state.reset_project()


# ── FastAPI app construction ───────────────────────────────────────


app: FastAPI = FastAPI(
    title="ctxr-fsm",
    version=ctxr.fsm.__version__,
    description=(
        "HTTP API for the ctxr.fsm SQLite-backed FSM substrate. "
        "Mirrors the MCP tool surface for REST/SSE clients."
    ),
    lifespan=lifespan_handler,
)


# CORS first — must be the outermost middleware so pre-flight OPTIONS
# requests are answered before any auth checks fire (browsers send
# OPTIONS without the ``Authorization`` header, which would otherwise
# trip ``require_auth`` and respond with 401 / 403 instead of the
# expected CORS headers).
#
# ``allow_origin_regex`` covers any loopback host on any port so the
# supervisor's ephemeral-port boots (Vite picks a random port when
# 5173 is taken by another dev process, common in e2e parallel runs)
# stay reachable from the InfoTopBar's ``/healthz`` probes. The dev
# API already trusts every request when ``CTXR_FSM_API_TOKEN`` is
# unset, so widening the CORS allowlist to loopback origins matches
# the existing dev-trust posture rather than relaxing the production
# bearer-auth one. The e2e harness benefits too: it spawns the UI on
# an ephemeral port and the browser reaches ``/healthz`` without each
# test having to wire ``$CTXR_FSM_API_CORS_ORIGINS`` per-port.
app.add_middleware(
    CORSMiddleware,
    allow_origins=_resolve_cors_origins(),
    allow_origin_regex=r"^http://(?:localhost|127\.0\.0\.1)(?::\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Pydantic response models ───────────────────────────────────────
# Every response is modelled so the generated OpenAPI schema at
# ``/docs`` is precise. New endpoints follow the same pattern.


class HealthResponse(BaseModel):
    """Shape of the ``/healthz`` response — liveness only."""

    status: str = Field(..., description="Constant ``ok`` when the process is up.")


class ReadinessResponse(BaseModel):
    """Shape of the ``/readyz`` response — readiness reports project state."""

    status: str = Field(..., description="``ok`` when ready, ``starting`` otherwise.")
    project_open: bool = Field(
        ...,
        description="Whether the SQLite :class:`Project` handle is bound and usable.",
    )


class SubsystemInfo(BaseModel):
    """One row of the ``ProjectMetadata.subsystems`` map.

    Mirrors the per-subsystem block written by the supervisor into
    ``.ctxr-fsm/active-mcp.json`` (mcp / api / ui). The UI topbar
    surfaces this so an operator can see at a glance which subsystem
    is up + click through to its endpoint (Swagger, the UI itself,
    the MCP SSE healthz, etc.) without leaving the dashboard.

    ``healthz_url`` is the probe the supervisor uses to verify the
    subsystem is live before writing the discovery doc, so the UI
    can re-probe it on demand to show "healthy / degraded" badges
    next to the URL. ``base_url`` is the human-clickable link the
    operator wants. ``pid`` is included so the UI can show
    "subsystem owned by pid 42 on this host", useful when two
    operators are debugging the same project from different
    terminals.
    """

    base_url: str = Field(..., description="Primary URL for this subsystem (the human-clickable one).")
    healthz_url: str | None = Field(
        None,
        description="Health probe URL. ``None`` when the subsystem doesn't expose a separate healthz path.",
    )
    pid: int | None = Field(
        None,
        description="Process ID of the subsystem worker. ``None`` when not reported by the supervisor.",
    )


class ProjectMetadata(BaseModel):
    """Metadata payload returned by ``GET /api/v1/projects/current``.

    W22 added ``project_root`` + ``db_path_relative`` so the UI
    topbar / Settings surface can render portable, committable
    project-relative paths instead of absolute filesystem strings.
    W22b3 extends the payload with the operator-facing project slug
    (read from the ``projects`` table) and the live ``subsystems``
    map read from ``.ctxr-fsm/active-mcp.json`` so the info-rich
    topbar can show "you're connected to ``my-project`` on
    ``/Users/dev/work/my-project`` with mcp+api+ui all green" as a
    single discovery roundtrip. Swagger is derived from the API's
    own base_url (``/docs``) rather than threaded through
    active-mcp.json because Swagger is just a route on the API
    process, not a separate subsystem.
    """

    fsm_version: str = Field(..., description="The ``ctxr.fsm`` package version.")
    project_open: bool = Field(..., description="Whether the :class:`Project` is bound.")
    project_slug: str | None = Field(
        None,
        description=(
            "The slug of the default project row in the ``projects``"
            " table. ``None`` when the projects table is empty (a"
            " freshly-migrated DB with no project rows yet — the seeded"
            " ``default`` row is created lazily by"
            " :meth:`Project.start_run`)."
        ),
    )
    swagger_url: str = Field(
        ...,
        description=(
            "Absolute URL of the API's auto-generated OpenAPI viewer."
            " Derived from the incoming request's ``base_url`` so the"
            " value is correct regardless of how the API is mounted"
            " (loopback, dev proxy, future reverse-proxy)."
        ),
    )
    api_base_url: str = Field(
        ...,
        description=(
            "Absolute base URL of this API. Same derivation as"
            " ``swagger_url``; exposed so the UI can build deep links"
            " into the JSON API without re-deriving the prefix."
        ),
    )
    subsystems: dict[str, SubsystemInfo] = Field(
        default_factory=dict,
        description=(
            "Subsystem-name -> :class:`SubsystemInfo` mapping read"
            " verbatim from ``.ctxr-fsm/active-mcp.json``. Empty when"
            " the supervisor hasn't written the discovery doc yet"
            " (cold boot before ``ctxr-fsm serve`` completes its"
            " healthz polls, or when ``project_root`` is None). The"
            " canonical subsystem names are ``mcp``, ``api``, ``ui``."
        ),
    )
    db_path: str = Field(
        ...,
        description=(
            "Absolute filesystem path of the open SQLite database when"
            " filesystem-backed (normalised via :meth:`Path.resolve` so"
            " the value is uniform across hosts regardless of how the"
            " engine was opened). For non-file backends (``:memory:``,"
            " ``file:``-URI variants), this is the raw"
            " ``engine.url.database`` segment — e.g. ``:memory:`` or"
            " ``file:test.db`` — so the operator sees exactly what"
            " SQLAlchemy resolved from the URL. When the URL has no"
            " ``database`` component at all (``sqlite://``), this"
            " falls back to the rendered ``str(engine.url)``. Clients"
            " distinguish a real path from a sentinel by checking"
            " ``project_root`` / ``db_path_relative`` for ``None``."
        ),
    )
    project_root: str | None = Field(
        None,
        description=(
            "Absolute path of the project root that hosts ``.ctxr-fsm/``."
            " Computed by walking up from the resolved DB path; falls"
            " back to the DB's parent directory when no ``.ctxr-fsm/``"
            " ancestor is found (operator passed a non-canonical"
            " ``--db``). ``None`` when the DB URL has no filesystem"
            " path (in-memory / non-file backends) — derivation is"
            " meaningless in that case."
        ),
    )
    db_path_relative: str | None = Field(
        None,
        description=(
            "Path of the open DB relative to ``project_root``. For the"
            " canonical layout this is ``.ctxr-fsm/fsm.db``. UI surfaces"
            " prefer this over ``db_path`` so the value stays portable"
            " across machines and committable to shared configs."
            " ``None`` when ``project_root`` is also ``None`` (paired"
            " field; see above)."
        ),
    )


# ── Health endpoints ───────────────────────────────────────────────
# Health probes are deliberately NOT behind ``require_auth``: kube /
# systemd / docker-healthcheck callers don't carry the token, and a
# 401 from ``/healthz`` would mark the pod unhealthy and trigger a
# restart loop. The probes leak no privileged information.


@app.get("/healthz", response_model=HealthResponse, tags=["health"])
def healthz() -> HealthResponse:
    """Liveness probe — returns ``ok`` if the ASGI app responds at all.

    Does not touch the database. A 200 here means "the Python
    process is alive and serving HTTP", nothing more — readiness
    (``/readyz``) is the probe that knows about the project handle.
    """
    return HealthResponse(status="ok")


@app.get("/readyz", response_model=ReadinessResponse, tags=["health"])
def readyz() -> ReadinessResponse:
    """Readiness probe — reports whether the project handle is bound.

    Returns 200 in both states (open / not open) so an orchestrator
    can distinguish "still starting up" from "crashed" by reading the
    body. Returning 503 when not ready is also reasonable, but the
    body-driven approach keeps the response useful for humans hitting
    the endpoint directly from a browser tab during dev.
    """
    open_ = _state.is_open()
    return ReadinessResponse(
        status="ok" if open_ else "starting",
        project_open=open_,
    )


# ── Project metadata ───────────────────────────────────────────────
# The first auth-guarded route. Trivial today; the UI uses it as a
# "did my token actually work?" probe before issuing the heavier
# discovery calls.


@app.get(
    "/api/v1/projects/current",
    response_model=ProjectMetadata,
    tags=["projects"],
    dependencies=[Depends(require_auth)],
)
def get_current_project(project: ProjectDep, request: Request) -> ProjectMetadata:
    """Return metadata about the currently open project.

    The :class:`Project` handle exposes the SQLAlchemy engine, whose
    URL carries the DB path — we surface it as a string so the UI
    can display "you are connected to ``…/fsm.db``" without needing
    a separate ``/settings`` endpoint just for the path. Future
    waves extend the payload with run counts, registered specs, and
    workspace metadata.
    """
    # ``engine.url`` is a SQLAlchemy URL object; its ``database``
    # attribute is the filesystem path for SQLite URLs. Falls back to
    # the rendered URL string so a future swap to a non-file backend
    # (memory, network) still returns something useful instead of
    # ``None`` — but in that case we cannot meaningfully derive a
    # project root / relative path, so the paired fields stay ``None``
    # rather than emit nonsense (e.g. treating ``sqlite://`` as a
    # filesystem path).
    db_url_database = project.engine.url.database
    # For non-file backends fall back to the rendered URL so the field
    # still has SOMETHING useful; for file backends we resolve below
    # so the doc-promised "absolute filesystem path" actually holds
    # even when the engine was opened with a relative ``--db`` arg.
    db_path = db_url_database or str(project.engine.url)
    project_root_str: str | None = None
    db_relative: str | None = None
    # `looks_like_filesystem_db_path` filters out :memory:, the URI
    # in-memory variant, and any other non-file sentinel SQLAlchemy
    # could expose — a plain `if db_url_database:` truthy check would
    # treat ':memory:' as a path and resolve it under cwd.
    if looks_like_filesystem_db_path(db_url_database):
        project_root_path, db_relative = project_root_and_relative(db_url_database)
        project_root_str = str(project_root_path)
        # Normalise db_path to absolute too — `project_root_and_relative`
        # already resolves the path internally, and the doc string says
        # this field is "absolute". When the engine was opened with a
        # relative path the raw url.database would be relative too,
        # contradicting the doc.
        db_path = str(Path(db_url_database).resolve())
    # Project slug — first row of the ``projects`` table. Schema reserves
    # multi-project mounts as a future extension; today there's at most
    # one project row per DB. ``None`` when the table is empty (lazy
    # seeded by ``Project.start_run``) — UI renders "no project yet"
    # rather than crashing the discovery call.
    project_slug: str | None = None
    try:
        with project.session_factory() as session:
            projects = project.projects.list(session)
        if projects:
            project_slug = projects[0].slug
    except Exception:
        # Never let metadata derivation fail the discovery route — the
        # UI's info-rich topbar would lose its slug for a transient DB
        # blip and the operator would see "no project yet" rather than
        # the real error context (the rest of the payload still lands).
        project_slug = None

    # Subsystem map — read the supervisor's discovery doc from
    # ``<project_root>/.ctxr-fsm/active-mcp.json``. Tolerate every
    # failure mode (file missing, malformed, partial keys) with an
    # empty map so the topbar shows "no subsystems reported" rather
    # than 500ing.
    # ``.ctxr-fsm/active-mcp.json`` writes each subsystem's primary URL
    # under the key ``http_url`` (see
    # :func:`ctxr.fsm.cli.lifecycle.supervisor._subsystem_payload`); the
    # API row additionally carries a ``docs_url``. We map both to
    # ``SubsystemInfo.base_url`` so the UI doesn't need to know about
    # the on-disk key vocabulary — its only contract is "give me a URL
    # I can show + a healthz URL I can probe". The original draft of
    # this route incorrectly looked for ``base_url`` and produced an
    # empty subsystems map even with a live supervisor.
    subsystems: dict[str, SubsystemInfo] = {}
    if project_root_str is not None:
        doc = read_active_mcp_file(Path(project_root_str))
        if doc is not None:
            raw_subsystems = doc.get("subsystems")
            if isinstance(raw_subsystems, dict):
                for name, block in raw_subsystems.items():
                    if not isinstance(block, dict):
                        continue
                    base = block.get("http_url")
                    if not isinstance(base, str) or not base:
                        continue
                    healthz = block.get("healthz_url")
                    pid = block.get("pid")
                    subsystems[str(name)] = SubsystemInfo(
                        base_url=base,
                        healthz_url=healthz if isinstance(healthz, str) else None,
                        pid=pid if isinstance(pid, int) else None,
                    )

    # Swagger + API base URL derivation. ``request.base_url`` is the
    # bare ASGI mount point (scheme + host + port + root_path); the
    # JSON routes in this app all live under the ``/api/v1`` prefix
    # (this route itself is at ``/api/v1/projects/current``), so
    # ``api_base_url`` MUST carry that prefix or every deep link the
    # UI builds against it will 404. The original draft of this route
    # returned the bare host URL, which produced broken links for
    # consumers building URLs off the field.
    #
    # Swagger lives at ``/docs`` on the FastAPI app — outside the
    # ``/api/v1`` prefix because FastAPI mounts the docs viewer at the
    # app root by default. We hard-code that suffix here rather than
    # importing the app inside its own route to read ``app.docs_url``.
    host_url = str(request.base_url).rstrip("/")
    api_base_url = f"{host_url}/api/v1"
    swagger_url = f"{host_url}/docs"

    return ProjectMetadata(
        fsm_version=ctxr.fsm.__version__,
        project_open=True,
        project_slug=project_slug,
        db_path=db_path,
        project_root=project_root_str,
        db_path_relative=db_relative,
        swagger_url=swagger_url,
        api_base_url=api_base_url,
        subsystems=subsystems,
    )


# ── Router mounts ──────────────────────────────────────────────────
# Feature routers are imported lazily at the bottom of the module so
# every router gets a fully-constructed ``app`` (with CORS, lifespan,
# and the always-on endpoints in place) to attach to. As additional
# routers land (runs, events/SSE), they slot in alongside the admin
# + specs routers here.

from ctxr.fsm.api.routes_admin import router as _admin_router
from ctxr.fsm.api.routes_events import router as _events_router
from ctxr.fsm.api.routes_runs import router as _runs_router
from ctxr.fsm.api.routes_specs import router as _specs_router

app.include_router(_admin_router)
app.include_router(_events_router)
app.include_router(_runs_router)
app.include_router(_specs_router)
