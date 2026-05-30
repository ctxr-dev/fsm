"""HTTP routes for FSM spec discovery + registration (``/api/v1/specs``).

This router is the W5 HTTP mirror of the W4 ``fsm.list_specs`` /
``fsm.register_spec`` MCP tools — same persistence layer, same
validation pipeline, different transport. It exists so REST/SSE
clients (the UI dev server, browser dashboards, third-party
orchestrators) can interact with the spec registry without speaking
MCP.

Endpoints
---------

* ``GET /api/v1/specs`` — every registered spec across every project,
  returned as :class:`SpecSummary` rows (no ``definition`` payload).
* ``GET /api/v1/specs/{slug}/versions`` — every registered version
  for the FSM whose natural id is ``slug``. Scoped by an optional
  ``project_slug`` query parameter (default ``"default"``) because
  the same FSM id can co-exist under different projects.
* ``GET /api/v1/specs/{spec_id}`` — full :class:`SpecDetail`
  including the canonical ``definition`` body. The ``spec_id`` here
  is the UUIDv7 row PK, not the natural slug — this collides
  visually with ``/specs/{slug}/versions`` but FastAPI's path
  matcher distinguishes them by the presence of the trailing
  ``/versions`` segment.
* ``POST /api/v1/specs`` — accept a JSON-encoded FsmSpec under
  ``definition`` plus an optional ``project_slug``, run the same
  schema + cross-cutting validation as the MCP tool, persist via
  :meth:`Project.register_spec`, and return a :class:`SpecRegistered`
  envelope.

Design notes
------------

* Read endpoints (GET) are wrapped in :func:`run_in_threadpool` even
  though SQLite calls are fast — FastAPI's event loop must stay
  unblocked, and the threadpool overhead is negligible compared to
  the JSON serialisation cost of a list of specs. Write endpoints
  follow the same pattern.
* Validation failures on POST return ``422 Unprocessable Entity``
  with a structured detail payload so clients can render
  per-field complaints (mirrors the MCP error envelope shape).
* Authentication is applied router-wide via
  ``dependencies=[Depends(require_auth)]``. Health probes live on
  the app itself (outside this router) and are unaffected.
* The router does NOT import anything from ``ctxr.fsm.mcp`` —
  this layer is HTTP-only and must not depend on the MCP SDK. It
  re-implements the small amount of shared logic (Pydantic
  validation + ``validate_fsm_spec`` + ``Project.register_spec``)
  directly against the core/sqlite modules.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from ctxr.fsm.api._deps import ProjectDep, require_auth
from ctxr.fsm.core.models import FsmSpec
from ctxr.fsm.core.spec import validate_fsm_spec
from ctxr.fsm.sqlite.models_core import FsmSpecTable, ProjectTable

__all__ = [
    "SpecDetail",
    "SpecRegisterBody",
    "SpecRegistered",
    "SpecSummary",
    "SpecVersion",
    "router",
]


# ---------------------------------------------------------------------------
# Pydantic response / request models
# ---------------------------------------------------------------------------
# We keep these local to the route module rather than re-using the MCP
# layer's ``SpecSummary`` / ``SpecRegisteredPayload`` types because (a)
# this module must not import from ``ctxr.fsm.mcp`` and (b) the HTTP
# wire shape may diverge from the MCP one in future waves without
# coupling either side.

_VO_CFG = ConfigDict(strict=True, extra="forbid")


class SpecSummary(BaseModel):
    """Trimmed spec row returned by ``GET /api/v1/specs``.

    Carries enough identity to build a follow-up detail URL
    (``/api/v1/specs/{id}``) plus the human-readable
    ``project_slug`` / ``slug`` / ``version`` triple the UI lists
    spec cards under. The full ``definition`` body is omitted to
    keep the wire payload bounded even for projects with many
    versions.
    """

    model_config = _VO_CFG

    id: str = Field(description="Row PK of the fsm_specs entry (UUIDv7 string).")
    project_id: str = Field(description="Row PK of the owning project row.")
    project_slug: str = Field(description="Human-readable slug of the owning project.")
    slug: str = Field(description="The FSM's own id, used as the natural slug.")
    version: int = Field(description="Monotonic version within (project_id, slug).")
    hash: str = Field(description="SHA-256 content hash of the canonical spec JSON.")
    created_at: str = Field(description="ISO-8601 timestamp of the registration.")


class SpecVersion(BaseModel):
    """One row of the ``GET /api/v1/specs/{slug}/versions`` payload.

    Identical shape to :class:`SpecSummary` except every row in the
    response shares the same ``slug`` and the same
    ``project_slug`` — the endpoint is a per-slug history view, so
    repeating those two fields per row is redundant on the wire but
    keeps each row self-describing for clients that flatten the
    response into a table.
    """

    model_config = _VO_CFG

    id: str = Field(description="Row PK of the fsm_specs entry (UUIDv7 string).")
    project_id: str = Field(description="Row PK of the owning project row.")
    project_slug: str = Field(description="Human-readable slug of the owning project.")
    slug: str = Field(description="The FSM's own id, used as the natural slug.")
    version: int = Field(description="Monotonic version within (project_id, slug).")
    hash: str = Field(description="SHA-256 content hash of the canonical spec JSON.")
    created_at: str = Field(description="ISO-8601 timestamp of the registration.")


class SpecDetail(BaseModel):
    """Full spec record returned by ``GET /api/v1/specs/{spec_id}``.

    Carries the canonical ``definition`` body parsed back into an
    :class:`FsmSpec` so the response is shape-checked end-to-end —
    clients that decode against this schema get strong typing for
    every field rather than an opaque ``dict[str, Any]``.
    """

    model_config = _VO_CFG

    id: str = Field(description="Row PK of the fsm_specs entry (UUIDv7 string).")
    project_id: str = Field(description="Row PK of the owning project row.")
    project_slug: str = Field(description="Human-readable slug of the owning project.")
    slug: str = Field(description="The FSM's own id, used as the natural slug.")
    version: int = Field(description="Monotonic version within (project_id, slug).")
    hash: str = Field(description="SHA-256 content hash of the canonical spec JSON.")
    definition: FsmSpec = Field(
        description="The full canonical FsmSpec definition (states, entry, …)."
    )
    registered_at: str = Field(description="ISO-8601 timestamp of the registration.")


class SpecRegisterBody(BaseModel):
    """Request body for ``POST /api/v1/specs``.

    ``definition`` is the raw JSON object form of the FsmSpec — we
    accept ``dict[str, Any]`` here (rather than ``FsmSpec``
    directly) so Pydantic's framework-level 422 response carries our
    own structured ``schema_validation_failed`` envelope instead of
    FastAPI's default per-field detail. Validation happens inside
    the handler.
    """

    model_config = _VO_CFG

    definition: dict[str, Any] = Field(
        description="Raw FsmSpec JSON body; validated server-side."
    )
    project_slug: str = Field(
        default="default",
        description="Slug of the owning project; created on first use.",
    )


class SpecRegistered(BaseModel):
    """Response shape for ``POST /api/v1/specs``.

    Mirrors the MCP tool's ``SpecRegisteredPayload``: just the
    identity of the registered row plus a ``created`` flag so the
    caller can distinguish a fresh insert from a hash-dedup match.
    The full ``definition`` is intentionally not echoed — the
    client already has it.
    """

    model_config = _VO_CFG

    spec_id: str = Field(description="Row PK of the registered fsm_specs entry.")
    hash: str = Field(description="SHA-256 canonical hash of the spec.")
    version: int = Field(description="Version assigned within (project, slug).")
    slug: str = Field(description="The FSM id used as the natural slug.")
    project_id: str = Field(description="Row PK of the owning project.")
    project_slug: str = Field(description="Human-readable slug of the owning project.")
    created: bool = Field(
        description="True iff a new row was inserted; False on hash-dedup match.",
    )


# ---------------------------------------------------------------------------
# Router construction
# ---------------------------------------------------------------------------
# Auth is applied router-wide so every spec endpoint requires the
# bearer token (in production mode). Dev mode (no ``CTXR_FSM_API_TOKEN``
# env var) trusts every caller — see :mod:`ctxr.fsm.api._auth`.

router = APIRouter(
    prefix="/api/v1",
    tags=["specs"],
    dependencies=[Depends(require_auth)],
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _list_specs_sync(project: Any) -> list[SpecSummary]:
    """Synchronous body of ``GET /api/v1/specs``.

    Pulled out so the async handler can hand it to
    :func:`run_in_threadpool` cleanly. The join on ``projects``
    keeps the response one round-trip (vs. N+1 lookups per spec).
    Ordered by ``(project_slug, slug, version)`` so the wire shape
    is deterministic for clients building UI lists.
    """
    stmt = (
        select(
            FsmSpecTable.id,
            FsmSpecTable.project_id,
            ProjectTable.slug.label("project_slug"),
            FsmSpecTable.slug,
            FsmSpecTable.version,
            FsmSpecTable.hash,
            FsmSpecTable.created_at,
        )
        .join(ProjectTable, FsmSpecTable.project_id == ProjectTable.id)
        .order_by(
            ProjectTable.slug.asc(),
            FsmSpecTable.slug.asc(),
            FsmSpecTable.version.asc(),
        )
    )
    with project.session_factory() as session:
        rows = session.execute(stmt).all()
    return [
        SpecSummary(
            id=row.id,
            project_id=row.project_id,
            project_slug=row.project_slug,
            slug=row.slug,
            version=row.version,
            hash=row.hash,
            created_at=row.created_at,
        )
        for row in rows
    ]


def _list_versions_sync(
    project: Any,
    slug: str,
    project_slug: str,
) -> list[SpecVersion]:
    """Synchronous body of ``GET /api/v1/specs/{slug}/versions``.

    Resolves ``project_slug`` to the owning project row first, then
    pulls every version row for ``(project.id, slug)`` ordered by
    ``version`` ascending. Returning an empty list when the project
    is unknown matches the "no versions registered" outcome — the
    caller's UI handles both identically.
    """
    with project.session_factory() as session:
        project_row = session.execute(
            select(ProjectTable).where(ProjectTable.slug == project_slug)
        ).scalar_one_or_none()
        if project_row is None:
            return []
        stmt = (
            select(
                FsmSpecTable.id,
                FsmSpecTable.project_id,
                FsmSpecTable.slug,
                FsmSpecTable.version,
                FsmSpecTable.hash,
                FsmSpecTable.created_at,
            )
            .where(
                FsmSpecTable.project_id == project_row.id,
                FsmSpecTable.slug == slug,
            )
            .order_by(FsmSpecTable.version.asc())
        )
        rows = session.execute(stmt).all()
    return [
        SpecVersion(
            id=row.id,
            project_id=row.project_id,
            project_slug=project_slug,
            slug=row.slug,
            version=row.version,
            hash=row.hash,
            created_at=row.created_at,
        )
        for row in rows
    ]


def _get_spec_sync(project: Any, spec_id: str) -> SpecDetail | None:
    """Synchronous body of ``GET /api/v1/specs/{spec_id}``.

    Returns ``None`` when no spec with the given PK exists so the
    async handler can raise a 404. We pull the project slug
    alongside the spec row in a single query — the UI needs the
    human-readable slug to render breadcrumbs and the alternative
    (a second round-trip) is wasteful.
    """
    stmt = (
        select(
            FsmSpecTable.id,
            FsmSpecTable.project_id,
            ProjectTable.slug.label("project_slug"),
            FsmSpecTable.slug,
            FsmSpecTable.version,
            FsmSpecTable.hash,
            FsmSpecTable.definition_json,
            FsmSpecTable.created_at,
        )
        .join(ProjectTable, FsmSpecTable.project_id == ProjectTable.id)
        .where(FsmSpecTable.id == spec_id)
    )
    with project.session_factory() as session:
        row = session.execute(stmt).first()
    if row is None:
        return None

    # ``definition_json`` is canonical JSON text — parse it back into
    # an FsmSpec so the response model carries a strongly-typed
    # definition. We use ``model_validate_json`` to avoid a redundant
    # ``json.loads`` round-trip.
    try:
        definition = FsmSpec.model_validate_json(row.definition_json)
    except ValidationError as exc:
        # The on-disk row failed to round-trip — almost certainly a
        # schema migration gap or a hand-edited row. Surface as a
        # 500 with a clear hint rather than silently returning a
        # half-baked detail object.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error": "spec_definition_corrupt",
                "spec_id": spec_id,
                "errors": exc.errors(),
            },
        ) from exc

    return SpecDetail(
        id=row.id,
        project_id=row.project_id,
        project_slug=row.project_slug,
        slug=row.slug,
        version=row.version,
        hash=row.hash,
        definition=definition,
        registered_at=row.created_at,
    )


def _register_spec_sync(
    project: Any,
    raw_definition: dict[str, Any],
    project_slug: str,
) -> SpecRegistered:
    """Synchronous body of ``POST /api/v1/specs``.

    Three-stage validation pipeline (matches the MCP tool):

    1. ``FsmSpec.model_validate`` — Pydantic schema check. Failures
       surface as 422 with the per-field ``errors()`` payload so
       clients can render structured complaints.
    2. :func:`validate_fsm_spec` — cross-cutting checks
       (reachability, dangling transitions, predicate parsability).
       Failures surface as 422 with the full
       :class:`FsmValidationResult` attached.
    3. :meth:`Project.register_spec` — the actual insert-or-dedupe.

    Returns the :class:`SpecRegistered` envelope on success. Any
    validation failure raises :class:`HTTPException` (422); the
    project handle being unbound surfaces from the dependency layer
    as a 500 well before this function runs.
    """
    # ── (1) Pydantic schema validation ────────────────────────────
    try:
        spec = FsmSpec.model_validate(raw_definition)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "invalid_spec_definition",
                "message": "failed to parse FsmSpec from definition",
                "errors": exc.errors(),
            },
        ) from exc

    # ── (2) Cross-cutting structural validation ───────────────────
    validation = validate_fsm_spec(spec)
    if not validation.valid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "schema_validation_failed",
                "message": "FsmSpec failed cross-cutting validation",
                "validation": validation.model_dump(mode="json"),
            },
        )

    # ── (3) Persist via the project facade ────────────────────────
    result = project.register_spec(spec, project_slug=project_slug)
    return SpecRegistered(
        spec_id=result.spec.id,
        hash=result.spec.hash,
        version=result.spec.version,
        slug=result.spec.slug,
        project_id=result.spec.project_id,
        project_slug=project_slug,
        created=result.created,
    )


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------
# Each handler is ``async def`` so FastAPI scheduling stays correct
# under load. The actual SQLite work runs in the default threadpool
# via :func:`run_in_threadpool` — SQLAlchemy's sync API is blocking
# and would otherwise stall the event loop while the DB is queried.


@router.get(
    "/specs",
    response_model=list[SpecSummary],
    summary="List every registered FSM spec",
    description=(
        "Returns a flat list of spec summaries across every project, "
        "ordered by (project_slug, slug, version). The full `definition` "
        "body is omitted; fetch it via `GET /api/v1/specs/{spec_id}`."
    ),
)
async def list_specs(project: ProjectDep) -> list[SpecSummary]:
    """List every registered spec across every project."""
    return await run_in_threadpool(_list_specs_sync, project)


@router.get(
    "/specs/{slug}/versions",
    response_model=list[SpecVersion],
    summary="List every version registered under a given FSM slug",
    description=(
        "Returns every registered version for the FSM whose natural id "
        "is `slug`, scoped to `project_slug` (default `'default'`), "
        "ordered by `version` ascending. An empty list means no "
        "versions are registered for that (project, slug) pair."
    ),
)
async def list_spec_versions(
    slug: str,
    project: ProjectDep,
    project_slug: str = "default",
) -> list[SpecVersion]:
    """List every registered version for ``(project_slug, slug)``."""
    return await run_in_threadpool(
        _list_versions_sync, project, slug, project_slug
    )


@router.get(
    "/specs/{spec_id}",
    response_model=SpecDetail,
    summary="Fetch a single registered FSM spec by its row id",
    description=(
        "Returns the full `SpecDetail` for the spec whose row PK is "
        "`spec_id`. Responds 404 when no spec with that id exists."
    ),
    responses={
        404: {
            "description": "No spec with the given id is registered.",
            "content": {
                "application/json": {
                    "example": {"detail": "spec not found"},
                },
            },
        },
    },
)
async def get_spec(spec_id: str, project: ProjectDep) -> SpecDetail:
    """Fetch a single spec by its row PK; 404 when unknown."""
    result = await run_in_threadpool(_get_spec_sync, project, spec_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"spec not found: {spec_id!r}",
        )
    return result


@router.post(
    "/specs",
    response_model=SpecRegistered,
    status_code=status.HTTP_201_CREATED,
    summary="Register an FSM spec (with validation + content-hash dedup)",
    description=(
        "Accepts a raw FsmSpec JSON body under `definition`, runs the "
        "Pydantic schema check plus cross-cutting structural "
        "validation (reachability, dangling transitions, loop "
        "done_field, predicate parsability), and persists via the "
        "project facade. Byte-identical re-registrations are "
        "idempotent (`created=False`). Validation failures respond "
        "422 with a structured detail envelope."
    ),
    responses={
        422: {
            "description": (
                "The submitted spec failed Pydantic schema validation "
                "or the cross-cutting structural checks."
            ),
        },
    },
)
async def register_spec(
    body: SpecRegisterBody,
    project: ProjectDep,
) -> SpecRegistered:
    """Validate and register a freshly-supplied FSM spec."""
    return await run_in_threadpool(
        _register_spec_sync,
        project,
        body.definition,
        body.project_slug,
    )
