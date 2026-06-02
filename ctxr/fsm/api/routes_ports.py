"""``ctxr.fsm.api.routes_ports`` — W23e port-change control surface.

Two endpoints:

* ``POST /api/v1/admin/ports`` — submit a request to change a
  subsystem's port. Validates the input, atomically writes a control
  file the supervisor watches, returns the new URL operators should
  expect after the restart settles.
* ``GET /api/v1/admin/ports/status/{request_id}`` — poll the supervisor's
  per-request status document.

The supervisor's watcher consumes the request, drains + respawns the
named subsystem, updates ``ports.json``, re-publishes
``active-mcp.json``, and writes the status doc with ``success`` or
``failed``. The UI polls the GET endpoint at 500ms cadence and acts
on each status (mount reconnecting overlay, redirect after success,
toast after failure).

Both endpoints inherit ``Depends(require_auth)`` from the parent
router so production-mode deployments cannot have an unauthenticated
operator-tier change.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from ctxr.fsm.api._deps import ProjectDep, require_auth
from ctxr.fsm.api._paths import (
    looks_like_filesystem_db_path,
    project_root_and_relative,
)
from ctxr.fsm.cli.lifecycle.primitives import (
    now_iso_ms,
    read_port_change_status,
    recall_port,
    write_port_change_request,
)
from ctxr.fsm.sqlite import Project

router = APIRouter(
    prefix="/api/v1/admin",
    tags=["admin", "ports"],
    dependencies=[Depends(require_auth)],
)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


SubsystemName = Literal["mcp", "api", "ui"]


class PortChangeRequestBody(BaseModel):
    """Wire shape for ``POST /api/v1/admin/ports``."""

    model_config = ConfigDict(strict=True, extra="forbid")

    subsystem: SubsystemName
    new_port: int = Field(
        ...,
        ge=1024,
        le=65535,
        description=(
            "Target TCP port (1024-65535). Privileged ports (<1024) are"
            " rejected client- AND server-side: the supervisor cannot bind"
            " them without root, and accepting the request would surface"
            " as a confusing 'failed: port_bound' downstream."
        ),
    )


class PortChangeAccepted(BaseModel):
    """Response for a successful ``POST /api/v1/admin/ports``."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    accepted: bool = True
    request_id: str
    subsystem: SubsystemName
    new_port: int
    old_port: int | None
    new_url_when_ready: str
    estimated_restart_ms: int
    status_poll_url: str


class PortChangeStatus(BaseModel):
    """Response for ``GET /api/v1/admin/ports/status/{request_id}``.

    ``status`` semantics:

    * ``pending`` — supervisor has acknowledged the request and is
      working through drain + respawn.
    * ``success`` — new port is bound + healthz passing.
    * ``failed`` — drain or respawn errored; ``error`` carries the
      structured reason.
    * ``unknown`` — no status document exists yet, OR the request_id
      doesn't match the latest status. The poller treats this as
      "keep polling for a few seconds; eventually escalate to
      supervisor-down".
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    request_id: str
    status: Literal["pending", "success", "failed", "unknown"]
    subsystem: SubsystemName | None = None
    old_port: int | None = None
    new_port: int | None = None
    new_url: str | None = None
    error: dict[str, object] | None = None
    started_at: str | None = None
    finished_at: str | None = None


# Runtime tuple form of ``PortChangeStatus.status`` for the on-disk
# coercion check in ``get_port_change_status``. Kept beside the model
# so a future status-vocabulary change touches both sites.
_PORT_CHANGE_STATUS_VALUES: tuple[str, ...] = (
    "pending",
    "success",
    "failed",
    "unknown",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _project_root_from_project(project: Project) -> Path:
    """Resolve the on-disk project root for the running API process.

    Without a filesystem-backed DB (in-memory backends, ``file:`` URIs)
    we cannot write to ``.ctxr-fsm/control/`` and the operator cannot
    use the port-change surface; return ``None``-like 503 in that case.
    """
    db_url_database = project.engine.url.database
    if not looks_like_filesystem_db_path(db_url_database):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "project root not resolvable; port-change requires a "
                "filesystem-backed project (got non-file DB backend). "
                "Use the CLI's port-change flow against a local supervisor "
                "instead."
            ),
        )
    project_root, _ = project_root_and_relative(db_url_database)
    return project_root


def _new_url_for_port(
    *, request: Request, subsystem: SubsystemName, new_port: int
) -> str:
    """Build the URL the operator should land on after the restart.

    Uses ``request.base_url`` so a Safari operator on ``localhost`` and
    a curl operator on ``127.0.0.1`` both get back the URL in the host
    form they actually use. Substituting the port preserves the rest
    of the URL contract (scheme, root_path, etc.) so cookies + origin-
    bound state survive a UI redirect.

    For ``api`` / ``mcp`` the base URL is the API host with the new
    port. For ``ui`` we build a same-scheme URL because the UI lives
    on a different origin from the API and the request didn't come
    through it; we rely on Vite's default ``http://`` scheme.
    """
    # request.base_url is always trailing-slashed and includes the
    # scheme + host + port. Strip the port + replace with the new one.
    base = str(request.base_url)
    # Parse out scheme + host without the port using a small split.
    # request.base_url's path component is just '/' (or the root_path)
    # so the host is in the URL's netloc.
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(base)
    host = parsed.hostname or "127.0.0.1"
    new_netloc = f"{host}:{new_port}"
    if subsystem == "ui":
        # UI lives on a different origin; preserve the same scheme but
        # use the host without forcing the API's port-derived netloc.
        return urlunparse((parsed.scheme, new_netloc, "", "", "", ""))
    return urlunparse((parsed.scheme, new_netloc, parsed.path or "/", "", "", ""))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/ports",
    response_model=PortChangeAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit a port-change request for the supervisor to process.",
)
async def submit_port_change(
    body: PortChangeRequestBody,
    request: Request,
    project: ProjectDep,
) -> PortChangeAccepted:
    """Queue a port-change request.

    Flow:
    1. Resolve the project root from the running project's DB path.
    2. Look up the current port via ``recall_port`` so the response
       carries an honest ``old_port`` (or ``None`` on first-time set).
    3. Reject no-op requests (new == old) with 422.
    4. Atomically write the control file. ``write_port_change_request``
       returns ``False`` when another request is already queued — the
       UI shows "another change is in flight, wait" without queueing
       overlapping work.
    5. Return the URL the operator should land on once healthz passes.
       The UI mounts its ReconnectingOverlay immediately and polls the
       status endpoint at 500ms cadence.
    """
    project_root = _project_root_from_project(project)
    old_port = recall_port(body.subsystem, project_root=project_root)
    if old_port is not None and old_port == body.new_port:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"{body.subsystem} is already on port {body.new_port}; "
                "nothing to do."
            ),
        )
    # Cross-subsystem collision: reject before queueing so the operator
    # doesn't sit through a 5-second overlay only to see a failure toast.
    # Soft check: ``recall_port`` only knows ports the supervisor has
    # already published, so a brand-new install or a never-started
    # mcp/ui returns None and a future bring-up can still race onto the
    # same port. True bind failures are surfaced by the supervisor as
    # ``status='failed'`` (with structured ``error`` describing the bind
    # collision) via the status document, and the overlay poller picks
    # them up that way.
    for other in ("mcp", "api", "ui"):
        if other == body.subsystem:
            continue
        other_port = recall_port(other, project_root=project_root)
        if other_port == body.new_port:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"port {body.new_port} is already in use by the "
                    f"{other!r} subsystem; pick another."
                ),
            )
    request_id = str(uuid.uuid4())
    payload = {
        "request_id": request_id,
        "subsystem": body.subsystem,
        "new_port": body.new_port,
        "requested_at": now_iso_ms(),
        "requestor": "ui-settings-form",
        "originating_url": str(request.base_url).rstrip("/"),
        "schema_version": 1,
    }
    accepted = write_port_change_request(payload, project_root=project_root)
    if not accepted:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "another port-change is already in flight; wait for the "
                "supervisor to consume it (typically a few seconds), or "
                "remove .ctxr-fsm/control/port-change.json manually to "
                "clear an orphaned request"
            ),
        )
    new_url = _new_url_for_port(
        request=request, subsystem=body.subsystem, new_port=body.new_port
    )
    base = str(request.base_url).rstrip("/")
    return PortChangeAccepted(
        request_id=request_id,
        subsystem=body.subsystem,
        new_port=body.new_port,
        old_port=old_port,
        new_url_when_ready=new_url,
        # mcp / api restarts: ~3s typical (drain + spawn + healthz);
        # ui restart: ~5s typical (vite cold-start). Caller uses the
        # value to size the "elapsed" counter on the overlay.
        estimated_restart_ms=5000 if body.subsystem == "ui" else 3000,
        status_poll_url=f"{base}/api/v1/admin/ports/status/{request_id}",
    )


@router.get(
    "/ports/status/{request_id}",
    response_model=PortChangeStatus,
    summary="Poll the status of a queued port-change request.",
)
async def get_port_change_status(
    request_id: str, project: ProjectDep
) -> PortChangeStatus:
    """Read the supervisor's per-request status document.

    Returns ``status='unknown'`` (not 404) when the on-disk document's
    ``request_id`` doesn't match — the UI polls aggressively and a
    brief eventual-consistency window between the API write returning
    and the supervisor writing pending is expected.
    """
    project_root = _project_root_from_project(project)
    doc = read_port_change_status(project_root)
    if doc is None or doc.get("request_id") != request_id:
        return PortChangeStatus(request_id=request_id, status="unknown")
    # Coerce any unrecognised on-disk ``status`` value to ``"unknown"``
    # so a malformed (or future-version) status document cannot escape
    # as a 500 to the poller. ``PortChangeStatus.status`` is a strict
    # ``Literal`` and would otherwise raise ``ValidationError`` on a
    # forwarded arbitrary string; that 500 falls outside the overlay's
    # 5xx-tolerant retry budget and surfaces as a hard error toast.
    raw_status = doc.get("status")
    coerced_status = cast(
        Literal["pending", "success", "failed", "unknown"],  # audit-strings: justified
        raw_status if raw_status in _PORT_CHANGE_STATUS_VALUES else "unknown",
    )
    return PortChangeStatus(
        request_id=request_id,
        status=coerced_status,
        subsystem=doc.get("subsystem"),
        old_port=doc.get("old_port"),
        new_port=doc.get("new_port"),
        new_url=doc.get("new_url"),
        error=doc.get("error"),
        started_at=doc.get("started_at"),
        finished_at=doc.get("finished_at"),
    )
