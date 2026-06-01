"""HTTP routes for the run lifecycle (``/api/v1/runs`` family).

This module owns the REST surface that mirrors the run-side of the
W4 MCP tool catalog (``fsm.list_runs``, ``fsm.get_run``,
``fsm.resume_run``, ``fsm.abort_run``). It exposes the same
data shapes the MCP layer already returns, served over plain JSON
HTTP so the UI dev server, browser-driven dashboards, and any
third-party orchestrator that does not speak MCP can drive the
substrate using a stock HTTP client.

Design contract
---------------

* Every endpoint declares ``Depends(get_project)`` and operates
  against the process-wide :class:`Project` handle bound at app
  start. Tests override the dependency through
  :attr:`FastAPI.dependency_overrides` rather than mutating module
  globals.
* Every endpoint is ``async def`` (FastAPI strongly prefers this so
  the event loop is never blocked by a sync route). The underlying
  SQLite calls are synchronous, so any call into a repo is wrapped
  in :func:`starlette.concurrency.run_in_threadpool` — this hands
  the work to the default threadpool and keeps the loop free for
  other connections (notably the SSE stream that lives next door).
* Every response model is a Pydantic class. For the value objects
  that already exist at the persistence layer
  (:class:`RunSummary`, :class:`StateNode`, :class:`Event` from
  :mod:`ctxr.fsm.sqlite`) we re-use them directly so the OpenAPI
  schema and the SQLite-side schema stay in lockstep without an
  intermediate translation layer that could silently drift.
* No MCP SDK imports. This layer is HTTP-only; the MCP layer is a
  parallel surface against the same substrate.
* Error semantics mirror MCP: a missing run returns HTTP 404 with a
  small JSON body (``{detail: "..."}``); a refused state transition
  (already-terminal abort, etc.) returns 409. All other failures
  surface as 500 with the exception text in ``detail`` — uvicorn's
  access log captures the traceback for follow-up.

Why ``run_in_threadpool`` for every read?
----------------------------------------

SQLite reads are typically sub-millisecond, but a few of the
projections here (``state_tree``, event filtering with no LIMIT)
can scan thousands of rows on large runs. Even a 10 ms blocking
call inside the event loop stalls every other connection — and
because the SSE endpoint (next module) holds long-lived
connections, a single slow read elsewhere can starve every
subscriber. Wrapping in the threadpool keeps the loop responsive
even when the database is under load. The threadpool overhead is
~50µs per call, which is negligible next to the SQL itself.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ctxr.fsm.api._deps import ProjectDep, require_auth
from ctxr.fsm.api._pagination import (
    MAX_PAGE_SIZE,
    Page,
    PageParams,
    make_page_params,
    paginate_sequence,
)
from ctxr.fsm.sqlite import (
    Event,
    JournalTxn,
    Lock,
    Project,
    Run,
    RunSummary,
)

# ``StateNode`` is sourced directly from :mod:`ctxr.fsm.sqlite.repos_core`
# rather than the package-level re-export. The package re-exports
# :class:`ctxr.fsm.sqlite.repos_states.StateNode` (which has the
# ``state`` + ``children`` shape used by the aggregator), but
# :meth:`RunsRepo.state_tree` — the producer for this route — returns
# the run-side :class:`ctxr.fsm.sqlite.repos_core.StateNode` (with
# ``entry_id`` / ``state_id`` / ``entry_seq`` / ``children``). Importing
# from the underlying module avoids the name collision and keeps the
# response-model validation in lockstep with the actual repo return type.
from ctxr.fsm.sqlite.repos_core import StateNode

__all__ = [
    "AbortBody",
    "AbortResult",
    "JournalRecovered",
    "ResumeBody",
    "ResumeResult",
    "RunDetail",
    "router",
]


# ── Router ──────────────────────────────────────────────────────────
# Auth is applied at router level so every endpoint inherits the
# bearer-token check without needing to redeclare it per route. In
# dev mode (``CTXR_FSM_API_TOKEN`` unset) the dependency is a no-op
# — see :mod:`ctxr.fsm.api._auth` for the predicate.
router: APIRouter = APIRouter(
    prefix="/api/v1",
    tags=["runs"],
    dependencies=[Depends(require_auth)],
)


# ── Statuses that the engine considers "incomplete" / "resumable" ──
# The repo layer (:mod:`ctxr.fsm.sqlite.repos_core`) keeps its own
# private copy of these sets to back ``RunsRepo.incomplete`` and
# ``RunsRepo.resumable``; we expose the *keywords* here so the route
# layer can dispatch to the right repo method without importing the
# private constants. A request for ``?status=incomplete`` is routed
# to :meth:`RunsRepo.incomplete`; ``?status=resumable`` to
# :meth:`RunsRepo.resumable`; everything else to
# :meth:`RunsRepo.by_status` (or :meth:`RunsRepo.latest` when no
# status is supplied at all).
_STATUS_INCOMPLETE: str = "incomplete"
_STATUS_RESUMABLE: str = "resumable"


# ── Per-route pagination factories ──────────────────────────────────
# Each list endpoint binds its own ``PageParams`` factory at module
# scope. The factory captures the default + allowed sort keys so the
# OpenAPI schema documents them and unknown keys 422 at the edge.
RunsPageParams = make_page_params(
    default_sort="last_update_at_desc",
    allowed_sorts=(
        "last_update_at_desc",
        "started_at_desc",
        "started_at_asc",
    ),
)

EventsPageParams = make_page_params(
    default_sort="seq_asc",
    allowed_sorts=("seq_asc", "seq_desc"),
)


# ── Pydantic response / request models ─────────────────────────────


class RunDetail(BaseModel):
    """The full per-run report returned by ``GET /runs/{run_id}``.

    Mirrors the W4 ``fsm.get_run`` payload field-for-field so a
    client can swap between MCP and HTTP without reshaping the
    response. ``manifest`` is the :class:`Run` value object dumped
    to a plain dict (Pydantic ``mode='json'`` so timestamps become
    strings); ``state_tree`` is the nested :class:`StateNode`
    rooted at the entry state, or ``None`` when no state entries
    have been recorded yet.

    ``events_count`` is the count of events recorded against the
    run rather than the events themselves — the dedicated
    ``/runs/{run_id}/events`` endpoint streams the journal with
    pagination, and embedding the full list here would force every
    "did this run finish?" probe to pay for the journal too. The
    count is cheap (one ``COUNT(*)`` round-trip) and answers the
    "is there anything to fetch?" question without the payload bloat.
    """

    model_config = ConfigDict(extra="forbid")

    manifest: dict[str, Any] = Field(
        ...,
        description=(
            "The full run row, dumped as a JSON-compatible dict. "
            "Mirrors :class:`ctxr.fsm.sqlite.Run`."
        ),
    )
    state_tree: StateNode | None = Field(
        default=None,
        description=(
            "Nested state-entry tree rooted at the entry state, "
            "or ``None`` when no state entries have been recorded."
        ),
    )
    events_count: int = Field(
        ...,
        description="Total number of events recorded against this run.",
    )
    journal: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Newest unfinalised journal txn for the run, or ``None`` "
            "when the run is quiescent. Mirrors the MCP shape."
        ),
    )
    lock: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Active lock for the run, or ``None`` when no lock is "
            "held. Mirrors the MCP shape."
        ),
    )


class ResumeBody(BaseModel):
    """Body for ``POST /runs/{run_id}/resume``.

    Both fields are optional — the engine's resume is a no-op if no
    ``from_state`` is supplied (it picks up at ``run.current_state``)
    and journal handling is only meaningful when an unfinalised txn
    exists for the run.
    """

    model_config = ConfigDict(extra="ignore")

    from_state: str | None = Field(
        default=None,
        description=(
            "Optional override of the state the engine should resume "
            "into. When ``None``, the engine continues from "
            "``run.current_state``."
        ),
    )
    journal_action: str | None = Field(
        default=None,
        description=(
            "How to handle any unfinalised journal txn: ``discard`` "
            "rolls it back, ``replay`` records replay intent for the "
            "engine to consume on wake-up. ``None`` leaves the journal "
            "alone."
        ),
    )


class ResumeResult(BaseModel):
    """Return value of ``POST /runs/{run_id}/resume``.

    Field-for-field mirror of :class:`ctxr.fsm.mcp.tools_runs.ResumeResult`
    so MCP and HTTP clients see the same payload. ``engine_resume`` is
    a human-readable hint pointing at the deferred engine-driven resume
    that lands in W12.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    from_state: str | None = None
    journal_action: str | None = None
    journal_txn_id: str | None = None
    engine_resume: str = (
        "engine-driven resume comes in a later workstream (W12)"
    )


class AbortBody(BaseModel):
    """Body for ``POST /runs/{run_id}/abort``."""

    model_config = ConfigDict(extra="ignore")

    reason: str | None = Field(
        default=None,
        description=(
            "Operator-supplied free-text reason. Recorded on the "
            "``run_aborted`` event payload for the audit trail."
        ),
    )


class AbortResult(BaseModel):
    """Return value of ``POST /runs/{run_id}/abort``.

    Mirrors :class:`ctxr.fsm.mcp.tools_runs.AbortResult` exactly.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    previous_status: str
    new_status: str = "aborted"
    ended_at: str
    reason: str | None = None


class JournalRecovered(BaseModel):
    """Return value of ``POST /runs/{run_id}/journal/{action}``.

    ``action`` is the verb that ran (``discard`` or ``replay``);
    ``txn_id`` is the journal txn that was acted upon, or ``None``
    when no unfinalised txn existed at the time of the call (in
    which case the operation is a no-op and ``acted=False``).
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    action: str
    acted: bool = Field(
        ...,
        description=(
            "Whether a journal txn was actually acted upon. ``False`` "
            "when no unfinalised txn existed for the run."
        ),
    )
    txn_id: str | None = None
    note: str | None = Field(
        default=None,
        description=(
            "Human-readable diagnostic. For ``replay``, points at the "
            "W12 deferral; for ``discard``, summarises the rollback."
        ),
    )


# ── Internal helpers ────────────────────────────────────────────────


def _lock_to_dict(lock: Lock | None) -> dict[str, Any] | None:
    """Render a :class:`Lock` as a plain JSON-compatible dict (or ``None``).

    Field-for-field identical to the MCP module's ``_lock_to_dict`` so
    the HTTP and MCP surfaces emit the same JSON. We re-implement
    locally rather than import to keep the MCP package out of the
    API's import graph (the module docstring spells this out).
    """
    if lock is None:
        return None
    return {
        "run_id": lock.run_id,
        "holder_session_id": lock.holder_session_id,
        "acquired_at": lock.acquired_at.isoformat(),
        "expires_at": lock.expires_at.isoformat(),
        "is_stale": lock.is_stale,
    }


def _journal_to_dict(txn: JournalTxn | None) -> dict[str, Any] | None:
    """Render a :class:`JournalTxn` as a plain JSON-compatible dict.

    Returns ``None`` when no txn is supplied so the field can be
    serialised straight into the response. Mirrors the MCP shape.
    """
    if txn is None:
        return None
    return {
        "id": txn.id,
        "run_id": txn.run_id,
        "status": txn.status,
        "staged_writes": list(txn.staged_writes),
        "started_at": txn.started_at.isoformat(),
        "ready_at": txn.ready_at.isoformat() if txn.ready_at else None,
        "finalised_at": (
            txn.finalised_at.isoformat() if txn.finalised_at else None
        ),
    }


def _within_session(project: Project, fn: Callable[[Session], Any]) -> Any:
    """Open a short-lived session, run ``fn`` inside it, return the result.

    Centralises the ``with project.session_factory() as session:``
    boilerplate so every helper that needs a read can express the
    intent as a one-liner. The session is read-only by convention —
    callers that need to write open their own ``session.begin()`` block.
    """
    with project.session_factory() as session:
        return fn(session)


# ── Endpoints ───────────────────────────────────────────────────────


@router.get(
    "/runs",
    response_model=Page[RunSummary],
    summary="List runs, optionally filtered.",
)
async def list_runs(
    project: ProjectDep,
    params: Annotated[PageParams, Depends(RunsPageParams)],
    run_status: str | None = Query(
        default=None,
        alias="status",
        description=(
            "Filter by status. ``incomplete`` / ``resumable`` are "
            "special keywords routed to the dedicated repo methods; "
            "any other value is matched literally against ``runs.status``."
        ),
    ),
    since: str | None = Query(
        default=None,
        description=(
            "ISO-8601 timestamp lower bound on ``last_update_at`` "
            "(inclusive). String comparison — the repo writes "
            "timestamps in a stable lexicographic format."
        ),
    ),
) -> Page[RunSummary]:
    """List runs with the same dispatch as the MCP ``fsm.list_runs`` tool.

    Routing rules:

    * No ``status`` → :meth:`RunsRepo.latest` (newest first by
      ``last_update_at``).
    * ``status=incomplete`` → :meth:`RunsRepo.incomplete`.
    * ``status=resumable`` → :meth:`RunsRepo.resumable`.
    * Any other ``status`` value → :meth:`RunsRepo.by_status`.

    Pagination is supplied via the standard ``page`` / ``page_size`` /
    ``sort`` triple (see :class:`PageParams`); the response is wrapped
    in :class:`Page` with the total row count derived after
    in-process filtering.

    ``since`` is applied in-process after the repo returns because the
    repo's convenience accessors do not (today) accept that parameter —
    extending them would be premature optimisation at the run-count
    scales this surface sees in practice (low thousands per project).
    """

    def _query(session: Session) -> list[RunSummary]:
        # Dispatch identical to the MCP tool so an MCP client and an
        # HTTP client see the same rows for the same query. The repo
        # methods already sort by ``last_update_at DESC``; we fetch the
        # full filtered set and let :func:`paginate_sequence` slice
        # the page so ``total`` reflects the true post-filter cardinality.
        if run_status is None:
            # ``latest`` requires an explicit limit; pass the
            # ``MAX_PAGE_SIZE`` upper bound the rest of the surface
            # honours so deep pagination still works without us
            # silently truncating at the old 50-row default.
            rows = project.runs.latest(session, limit=MAX_PAGE_SIZE)
        elif run_status == _STATUS_INCOMPLETE:
            rows = project.runs.incomplete(session)
        elif run_status == _STATUS_RESUMABLE:
            rows = project.runs.resumable(session)
        else:
            rows = project.runs.by_status(session, run_status)

        if since is not None:
            # Lexicographic comparison — see the repo's docstring for
            # the timestamp format guarantee.
            rows = [row for row in rows if row.last_update_at >= since]

        # Apply the user-supplied sort. The repo returns rows already
        # sorted by ``last_update_at DESC`` (matching the default), so
        # only re-sort when the caller asked for something different.
        if params.sort == "started_at_desc":
            rows = sorted(rows, key=lambda r: r.started_at, reverse=True)
        elif params.sort == "started_at_asc":
            rows = sorted(rows, key=lambda r: r.started_at)
        # ``last_update_at_desc`` (default) — repo already returned in
        # this order, no re-sort needed.
        return rows

    rows = await run_in_threadpool(_within_session, project, _query)
    return paginate_sequence(rows, params=params)


@router.get(
    "/runs/{run_id}",
    response_model=RunDetail,
    summary="Return the full per-run report.",
)
async def get_run(run_id: str, project: ProjectDep) -> RunDetail:
    """Return the manifest + state tree + counts + journal + lock.

    404 when ``run_id`` does not exist. The state tree and journal
    are returned as their underlying value objects (Pydantic) /
    plain dicts; the lock is serialised through ``_lock_to_dict``
    so the MCP and HTTP wire formats match field-for-field.
    """

    def _fetch(session: Session) -> dict[str, Any] | None:
        run: Run | None = project.runs.get(session, run_id)
        if run is None:
            return None
        tree = project.runs.state_tree(session, run_id)
        # ``events`` returns an iterator; we only need the count
        # here, so consume it lazily inside ``sum``. For very large
        # journals we could swap to a dedicated COUNT(*) repo method
        # later — at current scales the iteration cost is negligible.
        events_count = sum(1 for _ in project.runs.events(session, run_id))
        journal = project.journal.inspect(session, run_id=run_id)
        lock = project.locks.inspect(session, run_id=run_id)
        return {
            "manifest": run.model_dump(mode="json"),
            "state_tree": tree,
            "events_count": events_count,
            "journal": _journal_to_dict(journal),
            "lock": _lock_to_dict(lock),
        }

    payload = await run_in_threadpool(_within_session, project, _fetch)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no run with id {run_id!r}",
        )
    return RunDetail(**payload)


@router.get(
    "/runs/{run_id}/state-tree",
    response_model=StateNode,
    summary="Return the run's state-entry tree.",
)
async def get_state_tree(run_id: str, project: ProjectDep) -> StateNode:
    """Return the nested state-entry tree for the run.

    404 when the run does not exist OR has no recorded state entries
    yet. The two are distinguishable by inspecting ``GET /runs/{id}``
    first; we collapse them here because either case means "no tree
    to show" and the UI surfaces the same affordance for both.
    """

    def _fetch(session: Session) -> StateNode | None:
        # Guard against a non-existent run so the 404 distinguishes
        # "unknown run" from "known run with no entries" — the
        # latter still raises 404 below but only after we have
        # confirmed the run row exists, so the detail message is
        # precise.
        run: Run | None = project.runs.get(session, run_id)
        if run is None:
            return None
        return project.runs.state_tree(session, run_id)

    tree = await run_in_threadpool(_within_session, project, _fetch)
    if tree is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"no state tree available for run {run_id!r} "
                "(run unknown or no state entries recorded yet)"
            ),
        )
    return tree


@router.get(
    "/runs/{run_id}/events",
    response_model=Page[Event],
    summary="Return events recorded against the run.",
)
async def list_events(
    run_id: str,
    project: ProjectDep,
    params: Annotated[PageParams, Depends(EventsPageParams)],
    since_seq: int | None = Query(
        default=None,
        ge=0,
        description=(
            "Exclusive sequence-number lower bound. Only events "
            "with ``seq > since_seq`` are returned."
        ),
    ),
    kinds: list[str] | None = Query(
        default=None,
        description=(
            "Optional whitelist of event kinds. Repeat the query "
            "param to whitelist multiple kinds (``?kinds=run_started"
            "&kinds=state_entered``)."
        ),
    ),
) -> Page[Event]:
    """Return a slice of the run's event journal.

    404 when the run does not exist. ``since_seq`` is exclusive
    (matches the repo's semantics); ``kinds`` is a whitelist filter
    applied at the SQL level. The default ordering is ``seq ASC``
    (oldest first) so the caller's cursor is monotonic — the next
    call passes the last seen ``seq`` as ``since_seq``. ``?sort=seq_desc``
    flips the order for "newest first" dashboard views.
    """

    def _fetch(session: Session) -> list[Event] | None:
        run: Run | None = project.runs.get(session, run_id)
        if run is None:
            return None
        # ``events`` returns an iterator; materialise into a list so
        # we can slice + page. The full filtered set is materialised
        # so ``total`` in the returned :class:`Page` reflects the
        # true count after ``since_seq`` / ``kinds`` filtering.
        events_iter = project.runs.events(
            session, run_id, since_seq=since_seq, kinds=kinds
        )
        out: list[Event] = list(events_iter)
        # Repo returns ``seq ASC`` already; only re-sort when the
        # caller flipped the direction.
        if params.sort == "seq_desc":
            # ``seq`` may be None in defensive cases; fall back to a
            # sentinel that orders nulls last in DESC mode.
            out = sorted(out, key=lambda e: (e.seq is None, e.seq), reverse=True)
        return out

    events = await run_in_threadpool(_within_session, project, _fetch)
    if events is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no run with id {run_id!r}",
        )
    return paginate_sequence(events, params=params)


@router.post(
    "/runs/{run_id}/resume",
    response_model=ResumeResult,
    summary="Resume a paused / faulted run.",
)
async def resume_run(
    run_id: str,
    body: ResumeBody,
    project: ProjectDep,
) -> ResumeResult:
    """Resume bookkeeping for a paused / faulted run.

    Mirrors the MCP ``fsm.resume_run`` tool: handles the journal
    side-effect (``discard`` / ``replay``) and emits the
    ``run_resumed`` event so subscribers see the operator's
    intent. Engine-driven resume itself lands in W12.

    404 when the run does not exist.
    """
    from ctxr.fsm.core import spec as _spec_module  # noqa: F401  (binds .hash)
    from ctxr.fsm.core.models import EventKind  # local: avoid import cycle

    def _run_resume(session: Session) -> dict[str, Any] | None:
        run: Run | None = project.runs.get(session, run_id)
        if run is None:
            return None
        # W12 layer-9: resolve the latest registered version under the
        # same slug (NOT just the row the run was started against).
        # Re-registering a v2 under the same slug must trip the lock
        # even when the run still references the original row's PK —
        # the lock is about "did the operator change the canonical
        # definition mid-flight".
        registered = project.specs.get(session, run.fsm_spec_id)
        current_hash: str | None = None
        if registered is not None:
            versions = project.specs.list_versions(
                session,
                project_id=registered.project_id,
                slug=registered.slug,
            )
            current_hash = versions[-1].hash if versions else registered.hash
        return {
            "status": run.status,
            "fsm_spec_hash": run.fsm_spec_hash,
            "current_hash": current_hash,
        }

    pre = await run_in_threadpool(_within_session, project, _run_resume)
    if pre is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no run with id {run_id!r}",
        )

    # W12 layer-9: spec-hash lock. Surface a 409 (same family as
    # ``already in terminal status``) with the structured detail
    # carrying both hashes so the UI can render the drift directly.
    if (
        pre["current_hash"] is not None
        and pre["fsm_spec_hash"] != pre["current_hash"]
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "fsm_spec_changed",
                "detail": "FSM spec hash changed since run started",
                "run_hash": pre["fsm_spec_hash"],
                "current_hash": pre["current_hash"],
            },
        )

    def _apply(session: Session) -> dict[str, Any]:
        journal_action: str | None = None
        journal_txn_id: str | None = None

        # Ensure the engine producer exists so the run_resumed event
        # is attributed correctly. The upsert is idempotent.
        producer = project.producers.upsert(
            session,
            kind="engine",
            name="fsm.runtime",
        )

        existing = project.journal.inspect(session, run_id=run_id)
        journal_txn_id = existing.id if existing is not None else None

        if body.journal_action == "discard" and existing is not None:
            project.journal.discard(session, txn_id=existing.id)
            journal_action = "discarded"
        elif body.journal_action == "replay" and existing is not None:
            # Replay-into-engine lands in W12; record intent only.
            journal_action = "replay_requested"
        elif body.journal_action is not None:
            journal_action = "noop_no_journal"

        project.events.emit(
            session,
            producer_id=producer.id,
            kind=EventKind.run_resumed.value,
            payload={
                "run_id": run_id,
                "from_state": body.from_state,
                "journal_action": journal_action,
                "journal_txn_id": journal_txn_id,
                "engine_resume_pending": True,
            },
            run_id=run_id,
        )
        return {
            "journal_action": journal_action,
            "journal_txn_id": journal_txn_id,
        }

    def _txn(session: Session) -> dict[str, Any]:
        with session.begin():
            return _apply(session)

    result = await run_in_threadpool(_within_session, project, _txn)
    return ResumeResult(
        run_id=run_id,
        from_state=body.from_state,
        journal_action=result["journal_action"],
        journal_txn_id=result["journal_txn_id"],
    )


@router.post(
    "/runs/{run_id}/abort",
    response_model=AbortResult,
    summary="Abort an in-flight run.",
)
async def abort_run(
    run_id: str,
    body: AbortBody,
    project: ProjectDep,
) -> AbortResult:
    """Mark a run as ``aborted`` and emit ``run_aborted``.

    Mirrors the MCP ``fsm.abort_run`` tool. Returns 404 when the
    run does not exist; 409 when the run is already in a terminal
    state (``completed`` / ``aborted``) — the same refusal the MCP
    layer encodes as ``invalid_state_transition``.
    """
    from ctxr.fsm.core.models import EventKind
    from ctxr.fsm.sqlite.repos_core import _iso_now_ms

    def _peek(session: Session) -> Run | None:
        return project.runs.get(session, run_id)

    run = await run_in_threadpool(_within_session, project, _peek)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no run with id {run_id!r}",
        )
    if run.status in {"completed", "aborted"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"run {run_id!r} is already in terminal status "
                f"{run.status!r}; refusing to abort"
            ),
        )

    now = _iso_now_ms()

    def _apply(session: Session) -> str | None:
        producer = project.producers.upsert(
            session,
            kind="engine",
            name="fsm.runtime",
        )
        updated = project.runs.update_status(
            session,
            run_id=run_id,
            status="aborted",
            ended_at=now,
        )
        if updated is None:
            return None
        project.events.emit(
            session,
            producer_id=producer.id,
            kind=EventKind.run_aborted.value,
            payload={
                "run_id": run_id,
                "reason": body.reason,
                "previous_status": run.status,
                "ended_at": now,
            },
            run_id=run_id,
        )
        return updated.status

    def _txn(session: Session) -> str | None:
        with session.begin():
            return _apply(session)

    new_status = await run_in_threadpool(_within_session, project, _txn)
    if new_status is None:
        # Race: run vanished between the peek and the update. Surface
        # as 404 so the client retries with a fresh list.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"run {run_id!r} disappeared mid-abort",
        )

    return AbortResult(
        run_id=run_id,
        previous_status=run.status,
        new_status=new_status,
        ended_at=now,
        reason=body.reason,
    )


@router.post(
    "/runs/{run_id}/journal/{action}",
    response_model=JournalRecovered,
    summary="Recover (discard or replay) the run's pending journal txn.",
)
async def journal_recover(
    run_id: str,
    action: str,
    project: ProjectDep,
) -> JournalRecovered:
    """Discard or replay the run's unfinalised journal txn.

    ``action`` must be either ``discard`` or ``replay``; any other
    value produces 400. ``discard`` rolls the txn back (deletes the
    row); ``replay`` rolls the txn forward by flipping its status to
    ``finalised`` — mirroring the W4 MCP tool ``fsm.recover_journal``
    contract. The actual re-execution of staged writes is the engine's
    job on next boot (W12); this surface only owns the status
    transition that arms the recovery loop. 404 when the run itself
    does not exist; the response carries ``acted=False`` when the run
    exists but no unfinalised txn was found. 409 when the caller asks
    to replay a txn that has not yet been marked ``ready_to_finalise``
    — a still-``pending`` row has no staged writes to finalise, so we
    refuse rather than silently flipping the status.
    """
    if action not in {"discard", "replay"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"journal action {action!r} is not supported; expected "
                "one of 'discard' or 'replay'"
            ),
        )

    def _peek(session: Session) -> Run | None:
        return project.runs.get(session, run_id)

    run = await run_in_threadpool(_within_session, project, _peek)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no run with id {run_id!r}",
        )

    def _apply(session: Session) -> dict[str, Any]:
        existing = project.journal.inspect(session, run_id=run_id)
        if existing is None:
            return {"acted": False, "txn_id": None, "note": None}

        if action == "discard":
            project.journal.discard(session, txn_id=existing.id)
            return {
                "acted": True,
                "txn_id": existing.id,
                "note": (
                    "journal txn discarded; the staged writes will not "
                    "be materialised"
                ),
            }

        # action == "replay"
        # Only ``ready_to_finalise`` rows are legal to roll forward;
        # a still-``pending`` row carries no staged writes and would
        # be a meaningless flip. Surface 409 so the caller knows the
        # txn is in the wrong lifecycle position rather than silently
        # doing nothing.
        if existing.status != "ready_to_finalise":
            return {
                "acted": False,
                "txn_id": existing.id,
                "note": (
                    f"cannot replay journal txn {existing.id!r}: "
                    f"status is {existing.status!r}; only "
                    "'ready_to_finalise' is replayable"
                ),
                "_conflict": True,
            }
        project.journal.finalise(session, txn_id=existing.id)
        return {
            "acted": True,
            "txn_id": existing.id,
            "note": (
                "journal txn finalised; the engine will re-materialise "
                "the staged writes on next boot (W12)"
            ),
        }

    def _txn(session: Session) -> dict[str, Any]:
        with session.begin():
            return _apply(session)

    result = await run_in_threadpool(_within_session, project, _txn)
    # Wrong-status replay surfaces as 409 (mirrors the MCP layer's
    # ``journal_replay_not_ready`` structured error). The note carries
    # the human-readable diagnostic so the API caller still sees the
    # detail string in the HTTPException body.
    if result.get("_conflict"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(result["note"]),
        )
    return JournalRecovered(
        run_id=run_id,
        action=action,
        acted=bool(result["acted"]),
        txn_id=result["txn_id"],
        note=result["note"],
    )
