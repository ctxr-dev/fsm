"""MCP tools that drive the FSM run lifecycle.

This module registers the ``fsm.*`` tools that an MCP client uses to
start, brief, advance, resume, abort, list, and inspect FSM runs. The
implementations are intentionally *plumbing-only* for W4: they validate
inputs, drive the pure engine in :mod:`ctxr.fsm.core.engine`, and
persist results through the W2 :class:`Project` facade. The hard
enforcement layer (two-phase commit tokens, cosignatures, off-allowlist
tool observation, drift pausing) is W12 — this module ships the
return-shape plumbing for those features (``CommitToken`` field on
``CommitResult``, ``confirm_commit`` tool) but stubs the actual
enforcement so the surface is stable before W12 wires it.

Design contract
---------------

* Every tool input is a Pydantic model declared in this file; every
  output is a Pydantic model (also declared here, except where the
  engine already returns one — :class:`Brief` is re-used as-is).
* Every tool body is wrapped in ``try/except`` and on failure returns
  an :class:`McpToolError`-shaped envelope (see :mod:`ctxr.fsm.mcp._errors`)
  instead of letting the exception propagate. Propagating would turn
  into a JSON-RPC error frame, which is a different code path that
  older clients do not have to handle.
* All logging goes to stderr — MCP stdio uses stdout for the JSON-RPC
  framing. The server entry-point pins the root logger to
  ``sys.stderr``; we just call ``logging.getLogger(__name__)`` here.
* The :class:`Project` handle is fetched via :func:`get_project` once
  per tool call (cheap — it's a module-global lookup); we do NOT cache
  it on this module because tests reset the binding between cases.
* No subprocess. No network. Every state change is a Python call into
  the W2 repos.

Why one module per tool group (``tools_runs.py``) instead of one per
tool? The eight tools here all share the same helpers (spec
reconstruction, current-state lookup, env materialisation, brief
construction) and operate on the same handful of repos. Splitting them
further would force every helper to be either re-implemented or
imported across module boundaries for no real benefit.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from ctxr.fsm.core.engine import advance as engine_advance
from ctxr.fsm.core.engine import build_brief
from ctxr.fsm.core.models import (
    Brief,
    EventKind,
    FsmSpec,
    PostValidationResultEntry,
    RunCtx,
    TransitionEvaluation,
)
from ctxr.fsm.mcp import mcp
from ctxr.fsm.mcp._errors import McpToolError, as_error
from ctxr.fsm.mcp._state import get_project
from ctxr.fsm.sqlite import Project
from ctxr.fsm.sqlite.models_core import RunTable, StateTable, TransitionTable
from ctxr.fsm.sqlite.repos_core import RunSummary, _iso_now_ms
from ctxr.fsm.sqlite.repos_locks_journal import JournalTxn, Lock

__all__ = [
    "AbortResult",
    "AbortRunInput",
    "CommitOutputsInput",
    "CommitResult",
    "ConfirmCommitInput",
    "ConfirmResult",
    "GetBriefInput",
    "GetRunInput",
    "ListRunsInput",
    "ResumeResult",
    "ResumeRunInput",
    "RunDetail",
    "RunStartedPayload",
    "StartRunInput",
]


# Module logger. The server entry-point configures the root logger to
# write to stderr (``_configure_stderr_logging``); we just pull a child
# of it here so log lines carry the ``ctxr.fsm.mcp.tools_runs`` name and
# never leak onto stdout.
_LOG = logging.getLogger(__name__)


# Producer identity used when this module emits engine-attributed
# events (state_entered, transition_taken, run_completed, …). Mirrors
# the identity used by :meth:`Project.start_run` and the CLI so the
# audit trail attributes every lifecycle emit to one logical producer
# regardless of which surface drove it.
_ENGINE_PRODUCER_KIND: str = "engine"
_ENGINE_PRODUCER_NAME: str = "fsm.runtime"


# ---------------------------------------------------------------------------
# Tool input / output models
# ---------------------------------------------------------------------------


# We keep the I/O models flat and strict-but-friendly: ``strict=True``
# rejects accidental int/str confusion, ``extra="ignore"`` on inputs
# lets the MCP client send forward-compatible extra fields without the
# tool blowing up. Outputs are extra="forbid" to keep the wire format
# minimal and self-documenting.
_IN_CFG = ConfigDict(strict=False, extra="ignore", populate_by_name=True)
_OUT_CFG = ConfigDict(strict=False, extra="forbid", populate_by_name=True)


class StartRunInput(BaseModel):
    """Arguments for :func:`fsm_start_run`."""

    model_config = _IN_CFG

    spec_id: uuid.UUID = Field(
        ..., description="UUID of a registered FSM spec to start a run for."
    )
    args: dict[str, Any] = Field(
        default_factory=dict,
        description="Free-form arguments threaded into the run env at start.",
    )


class RunStartedPayload(BaseModel):
    """Return value of :func:`fsm_start_run`."""

    model_config = _OUT_CFG

    run_id: uuid.UUID
    brief: Brief
    fsm_spec_hash: str


class GetBriefInput(BaseModel):
    """Arguments for :func:`fsm_get_brief`."""

    model_config = _IN_CFG

    run_id: uuid.UUID


class CommitOutputsInput(BaseModel):
    """Arguments for :func:`fsm_commit_outputs`."""

    model_config = _IN_CFG

    run_id: uuid.UUID
    outputs: dict[str, Any]
    signature: str | None = Field(
        default=None,
        description=(
            "Optional commit signature. Recorded as-is in W4; cryptographic "
            "verification is wired in W12 alongside the cosignature surface."
        ),
    )


class CommitToken(BaseModel):
    """Slim mirror of :class:`ctxr.fsm.core.models.CommitToken` for the wire.

    The core type carries the same fields; redeclaring it here keeps the
    MCP module self-contained (so the generated tool schema does not
    pull in a parallel core import) and lets W12 evolve the token shape
    without touching every MCP consumer's generated bindings.
    """

    model_config = _OUT_CFG

    token: uuid.UUID
    run_id: uuid.UUID
    state_id: str
    expected_next_state: str
    expires_at: datetime


class CommitResult(BaseModel):
    """Discriminated result returned by :func:`fsm_commit_outputs`.

    ``kind`` mirrors the engine's :class:`EngineAdvanceResult` taxonomy
    but with one rename: the engine's ``"advance"`` becomes ``"advanced"``
    here to match the JS legacy MCP contract. ``"loop_continue"``
    likewise becomes ``"loop_continued"``.

    For W4 ``token`` is always ``None``; W12 will mint a
    :class:`CommitToken` and require the client to call
    :func:`fsm_confirm_commit` before the next brief is materialised.
    """

    model_config = _OUT_CFG

    kind: Literal["advanced", "terminal", "fault", "loop_continued"]
    brief: Brief | None = None
    next_state: str | None = None
    iteration_n: int | None = None
    verdict: Any = None
    reason: str | None = None
    errors: list[str] = Field(default_factory=list)
    evaluations: list[TransitionEvaluation] = Field(default_factory=list)
    post_validations: list[PostValidationResultEntry] = Field(default_factory=list)
    token: CommitToken | None = None


class ConfirmCommitInput(BaseModel):
    """Arguments for :func:`fsm_confirm_commit`."""

    model_config = _IN_CFG

    token: uuid.UUID
    expected_next_state: str


class ConfirmResult(BaseModel):
    """Return value of :func:`fsm_confirm_commit`.

    W4 always returns ``confirmed=True``; the real two-phase commit
    semantics land in W12. ``note`` documents that for any caller that
    is reading the surface today expecting hard enforcement.
    """

    model_config = _OUT_CFG

    confirmed: bool
    note: str | None = None


class ResumeRunInput(BaseModel):
    """Arguments for :func:`fsm_resume_run`."""

    model_config = _IN_CFG

    run_id: uuid.UUID
    from_state: str | None = None
    journal: Literal["discard", "replay"] | None = None


class ResumeResult(BaseModel):
    """Return value of :func:`fsm_resume_run`.

    Mirrors the CLI's ``ctxr-fsm run resume`` JSON payload exactly so a
    script that reads either surface can hand the result through one
    parser. ``engine_resume`` is a human-readable note pointing at the
    W12 deferral.
    """

    model_config = _OUT_CFG

    run_id: uuid.UUID
    from_state: str | None = None
    journal_action: str | None = None
    journal_txn_id: str | None = None
    engine_resume: str = (
        "engine-driven resume comes in a later workstream (W12)"
    )


class AbortRunInput(BaseModel):
    """Arguments for :func:`fsm_abort_run`."""

    model_config = _IN_CFG

    run_id: uuid.UUID
    reason: str | None = None


class AbortResult(BaseModel):
    """Return value of :func:`fsm_abort_run`."""

    model_config = _OUT_CFG

    run_id: uuid.UUID
    previous_status: str
    new_status: str = "aborted"
    ended_at: str
    reason: str | None = None


class ListRunsInput(BaseModel):
    """Arguments for :func:`fsm_list_runs`."""

    model_config = _IN_CFG

    filter_status: str | None = None
    since: str | None = None
    limit: int = Field(default=50, ge=1, le=500)


class GetRunInput(BaseModel):
    """Arguments for :func:`fsm_get_run`."""

    model_config = _IN_CFG

    run_id: uuid.UUID


class RunDetail(BaseModel):
    """Return value of :func:`fsm_get_run`.

    A read-only bundle of every "show me this run" projection. Fields
    map 1:1 onto the CLI's ``ctxr-fsm run show`` JSON output so the two
    surfaces stay aligned.
    """

    model_config = _OUT_CFG

    manifest: dict[str, Any]
    state_tree: dict[str, Any] | None = None
    events: list[dict[str, Any]] = Field(default_factory=list)
    journal: dict[str, Any] | None = None
    locks: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _ensure_engine_producer(project: Project) -> str:
    """Upsert the engine producer and return its id.

    Mirrors the lazy upsert performed by :meth:`Project.start_run` and
    the CLI so every lifecycle emit from this module is attributed to
    the same logical producer (``kind='engine'``, ``name='fsm.runtime'``)
    as the events that started / advanced the run.
    """
    with project.session_factory() as session, session.begin():
        producer = project.producers.upsert(
            session,
            kind=_ENGINE_PRODUCER_KIND,
            name=_ENGINE_PRODUCER_NAME,
        )
    return producer.id


def _load_fsm_spec(project: Project, registered_spec_id: str) -> FsmSpec | None:
    """Reconstruct an :class:`FsmSpec` from the row identified by id.

    The W2 substrate stores the spec as a canonical JSON ``definition``
    column; we reload it via Pydantic so the engine sees the same
    object shape it would see if the caller had constructed the spec
    in-process. Returns ``None`` when the spec row is missing.
    """
    with project.session_factory() as session:
        registered = project.specs.get(session, registered_spec_id)
    if registered is None:
        return None
    return FsmSpec.model_validate(registered.definition)


def _current_state_id(run_manifest: Any, spec: FsmSpec) -> str:
    """Return the run's current FSM state id, falling back to ``spec.entry``.

    The runs table tracks ``current_state`` as a nullable TEXT column;
    on a fresh run (before any state has been entered) it is ``None``,
    in which case the entry state from the spec is the right answer.
    Older runs from before the W4 bookkeeping landed may also leave it
    ``None`` mid-flight — falling back to the entry state is the
    conservative behaviour (worst case the brief targets the entry
    state and the operator notices the staleness).
    """
    if run_manifest.current_state:
        return str(run_manifest.current_state)
    return spec.entry


def _materialise_env(project: Project, run_manifest: Any) -> dict[str, Any]:
    """Reconstruct the run env from the run's args + emitted exit traces.

    The engine wants an env dict keyed by name. W2 stores the run-level
    seed args on the run row and outputs on per-state-entry rows; we
    merge them here so a brief built for the current state sees inputs
    from every prior exit.

    Iteration order: ``args`` first (lowest precedence), then state
    exits in ``entry_seq`` order so later outputs shadow earlier ones
    with the same key. The behaviour mirrors what the CLI / engine do
    in-memory when driving a run end-to-end.
    """
    env: dict[str, Any] = dict(run_manifest.args or {})
    with project.session_factory() as session:
        for state_entry in project.states.list_by_run(session, run_manifest.id):
            if state_entry.outputs:
                env.update(state_entry.outputs)
    return env


def _persist_state_entry(
    project: Project,
    *,
    run_id: str,
    state_id: str,
    inputs: dict[str, Any],
    producer_id: str,
    iteration_n: int | None = None,
) -> str:
    """Create a state-entry row, update ``runs.current_state``, emit the event.

    Returns the new state-entry row's PK so the caller can use it as the
    ``from_state_pk`` for the *next* transition row. The work happens
    inside one transaction so a crash mid-way leaves the run consistent.
    """
    with project.session_factory() as session, session.begin():
        next_seq = project.states.next_entry_seq(session, run_id)
        state_row = project.states.create(
            session,
            run_id=run_id,
            state_id=state_id,
            inputs=inputs,
            entry_seq=next_seq,
        )

        # Update the runs.current_state pointer + bump last_update_at.
        # No dedicated repo method exists for "set current_state", so we
        # mutate the ORM row directly; the @atomic block makes the write
        # visible together with the new state-entry row.
        run_row = session.get(RunTable, run_id)
        if run_row is not None:
            run_row.current_state = state_id
            run_row.last_update_at = _iso_now_ms()
            session.add(run_row)

        project.events.emit(
            session,
            producer_id=producer_id,
            kind=EventKind.state_entered.value,
            payload={
                "run_id": run_id,
                "state_id": state_id,
                "entry_seq": next_seq,
                "iteration_n": iteration_n,
            },
            run_id=run_id,
        )
    return state_row.id


def _record_state_exit(
    project: Project,
    *,
    run_id: str,
    state_pk: str,
    outputs: dict[str, Any],
    producer_id: str,
) -> None:
    """Mark a state entry as exited, persist its outputs, emit the event."""
    with project.session_factory() as session, session.begin():
        project.states.mark_exited(session, state_pk, outputs)
        project.events.emit(
            session,
            producer_id=producer_id,
            kind=EventKind.state_exited.value,
            payload={"run_id": run_id, "state_pk": state_pk},
            run_id=run_id,
        )


def _record_transition(
    project: Project,
    *,
    run_id: str,
    from_state_pk: str,
    to_state_id: str,
    kind: str,
    predicate: str | None,
    predicate_result: bool | None,
    producer_id: str,
) -> None:
    """Insert a transitions row + emit ``transition_taken``."""
    with project.session_factory() as session, session.begin():
        project.transitions.create(
            session,
            run_id=run_id,
            from_state_pk=from_state_pk,
            to_state_id=to_state_id,
            kind=kind,
            predicate=predicate,
            predicate_result=predicate_result,
        )
        project.events.emit(
            session,
            producer_id=producer_id,
            kind=EventKind.transition_taken.value,
            payload={
                "run_id": run_id,
                "from_state_pk": from_state_pk,
                "to_state_id": to_state_id,
                "kind": kind,
                "predicate": predicate,
                "predicate_result": predicate_result,
            },
            run_id=run_id,
        )


def _current_state_pk(project: Project, run_id: str, state_id: str) -> str | None:
    """Return the row PK of the *most recent* entry for ``state_id``.

    Used by ``commit_outputs`` to know which state-entry row to
    ``mark_exited`` and which row's PK to use as ``from_state_pk`` on
    the outgoing transition. We pick the latest by ``entry_seq DESC``
    so a re-entered state's prior visits are not accidentally mutated.
    """
    with project.session_factory() as session:
        stmt = (
            select(StateTable)
            .where(StateTable.run_id == run_id)
            .where(StateTable.state_id == state_id)
            .order_by(StateTable.entry_seq.desc())
            .limit(1)
        )
        row = session.execute(stmt).scalar_one_or_none()
    return row.id if row is not None else None


def _lock_to_dict(lock: Lock | None) -> dict[str, Any] | None:
    """Project a :class:`Lock` value object into the wire dict.

    Returns ``None`` when the lock is not held. The datetimes are
    rendered as ISO strings so the JSON survives a round-trip without
    needing per-field decoders on the client.
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
    """Project a :class:`JournalTxn` into the wire dict (or ``None``)."""
    if txn is None:
        return None
    return {
        "id": txn.id,
        "run_id": txn.run_id,
        "status": txn.status,
        "staged_writes": list(txn.staged_writes),
        "started_at": txn.started_at.isoformat(),
        "ready_at": txn.ready_at.isoformat() if txn.ready_at else None,
        "finalised_at": txn.finalised_at.isoformat() if txn.finalised_at else None,
    }


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool(
    name="fsm.start_run",
    description=(
        "Start a new FSM run against a registered spec. Returns the new "
        "run id, the brief for the entry state, and the spec hash recorded "
        "against the run."
    ),
)
def fsm_start_run(input: StartRunInput) -> RunStartedPayload | McpToolError:
    """Implement the ``fsm.start_run`` tool.

    Flow:
    1. Resolve the registered spec; 404-style error if missing.
    2. Reconstruct the :class:`FsmSpec` so the engine has a real object
       to introspect.
    3. Call :meth:`Project.start_run` to mint the run row, register the
       engine producer, and emit ``run_started``.
    4. Persist the entry-state row (W4 bookkeeping the engine itself
       does not perform yet), update ``runs.current_state``, and emit
       ``state_entered``.
    5. Build the first :class:`Brief` from the entry state and the
       run's seed args.
    """
    try:
        project = get_project()
        spec_id_str = str(input.spec_id)

        spec = _load_fsm_spec(project, spec_id_str)
        if spec is None:
            return as_error(
                "spec_not_found",
                detail=f"no registered spec with id {spec_id_str!r}",
                spec_id=spec_id_str,
            )

        run = project.start_run(spec_id=spec_id_str, args=dict(input.args))
        producer_id = _ensure_engine_producer(project)

        # Materialise the entry state's inputs from args + emit
        # state_entered so the audit trail shows the entry.
        entry_state = spec.get_state(spec.entry)
        worker = entry_state.worker or (
            entry_state.loop.worker if entry_state.loop is not None else None
        )
        entry_inputs: dict[str, Any] = {}
        if worker is not None:
            entry_inputs = {name: input.args.get(name) for name in worker.inputs}

        _persist_state_entry(
            project,
            run_id=run.id,
            state_id=entry_state.id,
            inputs=entry_inputs,
            producer_id=producer_id,
        )

        # Build the first brief. ``env`` here is just the seed args —
        # there are no prior state exits to merge in yet.
        brief = build_brief(
            spec,
            entry_state,
            env=dict(input.args),
            run_id=uuid.UUID(run.id),
        )

        return RunStartedPayload(
            run_id=uuid.UUID(run.id),
            brief=brief,
            fsm_spec_hash=run.fsm_spec_hash,
        )
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        _LOG.exception("fsm.start_run failed")
        return as_error("internal_error", detail=str(exc))


@mcp.tool(
    name="fsm.get_brief",
    description=(
        "Return the current Brief for an in-flight run. Builds the brief "
        "from the run's current state and merged env (args + prior state "
        "exit outputs)."
    ),
)
def fsm_get_brief(input: GetBriefInput) -> Brief | McpToolError:
    """Implement ``fsm.get_brief`` — build the brief for the current state."""
    try:
        project = get_project()
        run_id_str = str(input.run_id)

        run = project.get_run(run_id_str)
        if run is None:
            return as_error(
                "run_not_found",
                detail=f"no run with id {run_id_str!r}",
                run_id=run_id_str,
            )

        spec = _load_fsm_spec(project, run.fsm_spec_id)
        if spec is None:
            return as_error(
                "spec_not_found",
                detail=f"run references missing spec {run.fsm_spec_id!r}",
                spec_id=run.fsm_spec_id,
            )

        current_state_id = _current_state_id(run, spec)
        try:
            state = spec.get_state(current_state_id)
        except KeyError:
            return as_error(
                "invalid_state",
                detail=(
                    f"run.current_state {current_state_id!r} is not present "
                    "in spec.states"
                ),
                state=current_state_id,
            )

        env = _materialise_env(project, run)
        brief = build_brief(spec, state, env=env, run_id=uuid.UUID(run.id))
        return brief
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        _LOG.exception("fsm.get_brief failed")
        return as_error("internal_error", detail=str(exc))


@mcp.tool(
    name="fsm.commit_outputs",
    description=(
        "Commit worker outputs for the run's current state. Drives the pure "
        "engine to validate, decide loop continuation, run post-validations, "
        "and resolve the outgoing transition. Persists the resulting state "
        "transition (W4 plumbing; W12 wraps this with two-phase commit)."
    ),
)
def fsm_commit_outputs(input: CommitOutputsInput) -> CommitResult | McpToolError:
    """Implement ``fsm.commit_outputs`` — single-phase advance for W4.

    Persistence rules applied here:
    * ``advanced``: exit the current state, record the transition, enter
      the next state, return the next brief.
    * ``loop_continued``: leave the current state-entry row alone, return
      the iteration's brief (no exit yet).
    * ``terminal``: exit the current state, update the run to
      ``completed`` with verdict, emit ``run_completed``.
    * ``fault``: leave the current state-entry row alone, emit
      ``validation_failed`` so the audit trail captures the diagnostic.

    Returns a :class:`CommitResult` discriminated on ``kind``.
    """
    try:
        project = get_project()
        run_id_str = str(input.run_id)

        run = project.get_run(run_id_str)
        if run is None:
            return as_error(
                "run_not_found",
                detail=f"no run with id {run_id_str!r}",
                run_id=run_id_str,
            )

        spec = _load_fsm_spec(project, run.fsm_spec_id)
        if spec is None:
            return as_error(
                "spec_not_found",
                detail=f"run references missing spec {run.fsm_spec_id!r}",
                spec_id=run.fsm_spec_id,
            )

        current_state_id = _current_state_id(run, spec)
        try:
            spec.get_state(current_state_id)
        except KeyError:
            return as_error(
                "invalid_state",
                detail=(
                    f"run.current_state {current_state_id!r} is not present "
                    "in spec.states"
                ),
                state=current_state_id,
            )

        env = _materialise_env(project, run)
        ctx = RunCtx(
            run_id=uuid.UUID(run.id),
            fsm_id=spec.id,
            current_state=current_state_id,
            env=env,
        )

        result = engine_advance(spec, ctx, dict(input.outputs))
        producer_id = _ensure_engine_producer(project)
        from_pk = _current_state_pk(project, run.id, current_state_id)

        if result.kind == "fault":
            # Audit-trail the failure so subscribers see it on the bus;
            # do NOT mark the state exited — the worker is expected to
            # retry or the operator to intervene.
            with project.session_factory() as session, session.begin():
                project.events.emit(
                    session,
                    producer_id=producer_id,
                    kind=EventKind.validation_failed.value,
                    payload={
                        "run_id": run.id,
                        "state": current_state_id,
                        "reason": result.reason,
                        "errors": list(result.errors),
                        "post_validations": [
                            entry.model_dump(mode="json")
                            for entry in result.post_validations
                        ],
                    },
                    run_id=run.id,
                )
            return CommitResult(
                kind="fault",
                reason=result.reason,
                errors=list(result.errors),
                evaluations=list(result.evaluations),
                post_validations=list(result.post_validations),
            )

        if result.kind == "loop_continue":
            # The loop body wants another iteration. Hand back the next
            # brief; the state-entry row stays open until the loop
            # actually terminates.
            return CommitResult(
                kind="loop_continued",
                brief=result.brief,
                iteration_n=result.iteration_n,
            )

        if result.kind == "terminal":
            # Mark the current state exited with its outputs, flip the
            # run to ``completed`` with the engine's verdict, emit
            # ``run_completed``.
            if from_pk is not None:
                _record_state_exit(
                    project,
                    run_id=run.id,
                    state_pk=from_pk,
                    outputs=dict(input.outputs),
                    producer_id=producer_id,
                )
            now = _iso_now_ms()
            with project.session_factory() as session, session.begin():
                project.runs.update_status(
                    session,
                    run_id=run.id,
                    status="completed",
                    ended_at=now,
                    verdict=str(result.verdict) if result.verdict is not None else None,
                )
                project.events.emit(
                    session,
                    producer_id=producer_id,
                    kind=EventKind.run_completed.value,
                    payload={
                        "run_id": run.id,
                        "verdict": result.verdict,
                        "ended_at": now,
                    },
                    run_id=run.id,
                )
            return CommitResult(
                kind="terminal",
                verdict=result.verdict,
                evaluations=list(result.evaluations),
            )

        # result.kind == "advance"
        # Find the winning transition's guard kind / predicate text from
        # the evaluations trace so the transitions row is faithful.
        winning_eval: TransitionEvaluation | None = None
        for ev in result.evaluations:
            if ev.result and ev.to == result.next_state:
                winning_eval = ev
                break

        if from_pk is not None:
            _record_state_exit(
                project,
                run_id=run.id,
                state_pk=from_pk,
                outputs=dict(input.outputs),
                producer_id=producer_id,
            )
            _record_transition(
                project,
                run_id=run.id,
                from_state_pk=from_pk,
                to_state_id=result.next_state or "",
                kind=(winning_eval.kind if winning_eval else "always") or "always",
                predicate=(winning_eval.expression if winning_eval else None),
                # ``always`` / ``otherwise`` carry no predicate_result;
                # everything else gets the boolean evaluation outcome.
                predicate_result=(
                    None
                    if winning_eval is None
                    or winning_eval.kind in {"always", "otherwise"}
                    else bool(winning_eval.result)
                ),
                producer_id=producer_id,
            )

        # Enter the next state and bump runs.current_state.
        next_state_id = result.next_state or ""
        next_state = spec.get_state(next_state_id)
        next_worker = next_state.worker or (
            next_state.loop.worker if next_state.loop is not None else None
        )
        merged_env = {**env, **dict(input.outputs)}
        next_inputs: dict[str, Any] = {}
        if next_worker is not None:
            next_inputs = {name: merged_env.get(name) for name in next_worker.inputs}
        _persist_state_entry(
            project,
            run_id=run.id,
            state_id=next_state_id,
            inputs=next_inputs,
            producer_id=producer_id,
        )

        return CommitResult(
            kind="advanced",
            brief=result.brief,
            next_state=result.next_state,
            evaluations=list(result.evaluations),
        )
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        _LOG.exception("fsm.commit_outputs failed")
        return as_error("internal_error", detail=str(exc))


@mcp.tool(
    name="fsm.confirm_commit",
    description=(
        "Confirm a previously-issued commit token. W4 stub: always returns "
        "confirmed=True. W12 wires the real two-phase commit semantics."
    ),
)
def fsm_confirm_commit(input: ConfirmCommitInput) -> ConfirmResult | McpToolError:
    """Implement ``fsm.confirm_commit`` — W4 stub for the W12 surface.

    The tool exists so MCP clients can already wire the two-call commit
    flow today; the actual token-issue / token-consume path lands in
    W12. We accept any token + expected_next_state and return
    ``confirmed=True`` plus a note documenting the deferral.
    """
    try:
        # Touch the args so a linter cannot flag them as unused; the
        # tool's contract is "accept these and return confirmed=True"
        # in W4, but we still validate that the inputs Pydantic-parsed.
        _ = input.token
        _ = input.expected_next_state
        return ConfirmResult(
            confirmed=True,
            note=(
                "two-phase commit semantics are W12; commit_outputs is "
                "currently single-phase and confirm_commit is a stub."
            ),
        )
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        _LOG.exception("fsm.confirm_commit failed")
        return as_error("internal_error", detail=str(exc))


@mcp.tool(
    name="fsm.resume_run",
    description=(
        "Resume a paused/faulted run. W4 ships the journal bookkeeping + "
        "emits run_resumed; engine-driven resume itself comes in W12."
    ),
)
def fsm_resume_run(input: ResumeRunInput) -> ResumeResult | McpToolError:
    """Implement ``fsm.resume_run`` — mirrors the CLI ``run resume``."""
    try:
        project = get_project()
        run_id_str = str(input.run_id)

        run = project.get_run(run_id_str)
        if run is None:
            return as_error(
                "run_not_found",
                detail=f"no run with id {run_id_str!r}",
                run_id=run_id_str,
            )

        producer_id = _ensure_engine_producer(project)
        journal_action: str | None = None
        journal_txn_id: str | None = None

        with project.session_factory() as session, session.begin():
            existing = project.journal.inspect(session, run_id=run.id)
            journal_txn_id = existing.id if existing is not None else None

            if input.journal == "discard" and existing is not None:
                project.journal.discard(session, txn_id=existing.id)
                journal_action = "discarded"
            elif input.journal == "replay" and existing is not None:
                # Replay-into-engine lands in W12; here we only record
                # the operator's intent so the event stream tells the
                # full story when the engine wakes back up.
                journal_action = "replay_requested"
            elif input.journal is not None:
                journal_action = "noop_no_journal"

            project.events.emit(
                session,
                producer_id=producer_id,
                kind=EventKind.run_resumed.value,
                payload={
                    "run_id": run.id,
                    "from_state": input.from_state,
                    "journal_action": journal_action,
                    "journal_txn_id": journal_txn_id,
                    "engine_resume_pending": True,
                },
                run_id=run.id,
            )

        return ResumeResult(
            run_id=uuid.UUID(run.id),
            from_state=input.from_state,
            journal_action=journal_action,
            journal_txn_id=journal_txn_id,
        )
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        _LOG.exception("fsm.resume_run failed")
        return as_error("internal_error", detail=str(exc))


@mcp.tool(
    name="fsm.abort_run",
    description=(
        "Mark a run as aborted and emit run_aborted. Refuses runs already "
        "in a terminal state (completed / aborted)."
    ),
)
def fsm_abort_run(input: AbortRunInput) -> AbortResult | McpToolError:
    """Implement ``fsm.abort_run`` — atomic update + run_aborted emit."""
    try:
        project = get_project()
        run_id_str = str(input.run_id)

        run = project.get_run(run_id_str)
        if run is None:
            return as_error(
                "run_not_found",
                detail=f"no run with id {run_id_str!r}",
                run_id=run_id_str,
            )
        if run.status in {"completed", "aborted"}:
            return as_error(
                "invalid_state_transition",
                detail=(
                    f"run {run_id_str!r} is already in terminal status "
                    f"{run.status!r}; refusing to abort"
                ),
                previous_status=run.status,
            )

        producer_id = _ensure_engine_producer(project)
        now = _iso_now_ms()

        with project.session_factory() as session, session.begin():
            updated = project.runs.update_status(
                session,
                run_id=run.id,
                status="aborted",
                ended_at=now,
            )
            if updated is None:
                return as_error(
                    "run_not_found",
                    detail=f"run {run_id_str!r} disappeared mid-abort",
                    run_id=run_id_str,
                )

            project.events.emit(
                session,
                producer_id=producer_id,
                kind=EventKind.run_aborted.value,
                payload={
                    "run_id": run.id,
                    "reason": input.reason,
                    "previous_status": run.status,
                    "ended_at": now,
                },
                run_id=run.id,
            )

        return AbortResult(
            run_id=uuid.UUID(run.id),
            previous_status=run.status,
            new_status="aborted",
            ended_at=now,
            reason=input.reason,
        )
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        _LOG.exception("fsm.abort_run failed")
        return as_error("internal_error", detail=str(exc))


@mcp.tool(
    name="fsm.list_runs",
    description=(
        "List runs, optionally filtered by status and since-timestamp. "
        "Returns slim RunSummary value objects; use fsm.get_run for the "
        "full per-run picture."
    ),
)
def fsm_list_runs(input: ListRunsInput) -> list[RunSummary] | McpToolError:
    """Implement ``fsm.list_runs`` — mirrors the CLI ``runs ls`` shortcuts.

    Routing rules match the CLI exactly: ``incomplete`` and ``resumable``
    are special keywords that hit dedicated repo methods; any other
    status falls through to ``by_status``; no status hits ``latest``.
    """
    try:
        project = get_project()
        with project.session_factory() as session:
            if input.filter_status is None:
                rows = project.runs.latest(session, limit=input.limit)
            elif input.filter_status == "incomplete":
                rows = project.runs.incomplete(session)
            elif input.filter_status == "resumable":
                rows = project.runs.resumable(session)
            else:
                rows = project.runs.by_status(session, input.filter_status)

        # ``--since`` is a lexicographic ISO comparison — the repo
        # writes timestamps with a stable suffix so string ordering ==
        # chronological ordering.
        if input.since is not None:
            rows = [row for row in rows if row.last_update_at >= input.since]
        return rows[: input.limit]
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        _LOG.exception("fsm.list_runs failed")
        return as_error("internal_error", detail=str(exc))


@mcp.tool(
    name="fsm.get_run",
    description=(
        "Return the full per-run report: manifest, state tree, last 50 "
        "events, journal, locks."
    ),
)
def fsm_get_run(input: GetRunInput) -> RunDetail | McpToolError:
    """Implement ``fsm.get_run`` — assemble the full run picture."""
    try:
        project = get_project()
        run_id_str = str(input.run_id)

        run = project.get_run(run_id_str)
        if run is None:
            return as_error(
                "run_not_found",
                detail=f"no run with id {run_id_str!r}",
                run_id=run_id_str,
            )

        with project.session_factory() as session:
            tree = project.runs.state_tree(session, run.id)
            event_rows = list(project.runs.events(session, run.id))
            journal = project.journal.inspect(session, run_id=run.id)
            lock = project.locks.inspect(session, run_id=run.id)

        recent_events = event_rows[-50:]

        return RunDetail(
            manifest=run.model_dump(mode="json"),
            state_tree=(
                tree.model_dump(mode="json") if tree is not None else None
            ),
            events=[event.model_dump(mode="json") for event in recent_events],
            journal=_journal_to_dict(journal),
            locks=_lock_to_dict(lock),
        )
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        _LOG.exception("fsm.get_run failed")
        return as_error("internal_error", detail=str(exc))


# Defensive: keep a small unused-import dam so static-analysis can't
# silently strip the TransitionTable import that we may want to use in
# follow-up iterations. The actual runtime cost is zero (TYPE_CHECKING
# would also work, but plain import keeps mypy / pyright honest about
# the module's existence at runtime).
_ = TransitionTable
