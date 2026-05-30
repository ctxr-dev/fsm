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
import os
import uuid
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from ctxr.fsm.cli.lifecycle.primitives import write_active_run_marker
from ctxr.fsm.core.engine import advance as engine_advance
from ctxr.fsm.core.engine import build_brief
from ctxr.fsm.core.models import (
    Brief,
    CommitSignature,
    EngineAdvanceKind,
    EventKind,
    FsmSpec,
    PostValidationResultEntry,
    RunCtx,
    TransitionEvaluation,
    TransitionKind,
    VerifierVerdict,
)
from ctxr.fsm.core.verifier import VerifierOutcome, run_verifier
from ctxr.fsm.mcp import mcp
from ctxr.fsm.mcp._drain_decorator import drain_aware
from ctxr.fsm.mcp._errors import McpToolError, as_error
from ctxr.fsm.mcp._shared_enums import JournalAction
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
    "CommitResultKind",
    "ConfirmCommitInput",
    "ConfirmResult",
    "GetBriefInput",
    "GetRunInput",
    "JournalAction",
    "ListRunsInput",
    "ResumeResult",
    "ResumeRunInput",
    "RunDetail",
    "RunStartedPayload",
    "StartRunInput",
]


class CommitResultKind(StrEnum):
    """Discriminator for :class:`CommitResult`.

    Mirrors the engine's :class:`EngineAdvanceKind` taxonomy but with
    one rename: ``advance`` becomes ``advanced`` and ``loop_continue``
    becomes ``loop_continued`` to match the JS legacy MCP wire contract.
    Members carry the literal wire strings so JSON serialisation stays
    byte-stable.
    """

    advanced = "advanced"
    loop_continued = "loop_continued"
    terminal = "terminal"
    fault = "fault"


# Module logger. The server entry-point configures the root logger to
# write to stderr (``_configure_stderr_logging``); we just pull a child
# of it here so log lines carry the ``ctxr.fsm.mcp.tools_runs`` name and
# never leak onto stdout.
_LOG = logging.getLogger(__name__)


# Env-var override for the active-run marker location. When unset we
# fall back to ``Path.cwd()`` — matching how the supervisor / doctor
# resolve ``project_root``. Tests rely on the override so a temp dir
# can serve as the project root without changing the worker's cwd.
_ACTIVE_RUN_PROJECT_ROOT_ENV: str = "CTXR_FSM_PROJECT_ROOT"


def _project_root_for_marker() -> Path:
    """Return the directory whose ``.ctxr-fsm/`` should hold the marker.

    Precedence:
    1. ``$CTXR_FSM_PROJECT_ROOT`` from the process environment (used by
       tests + by operators with a non-cwd project layout).
    2. The current working directory (matches every other lifecycle
       primitive: supervisor, doctor, default DB path resolution).
    """
    override = os.environ.get(_ACTIVE_RUN_PROJECT_ROOT_ENV)
    if override:
        return Path(override).expanduser().resolve()
    return Path.cwd().resolve()


def _publish_active_run_marker(
    *,
    run_id: str | None,
    spec: FsmSpec | None,
    state_id: str | None,
) -> None:
    """Best-effort writer for the active-run marker (W12 layer-4 hook).

    ``run_id=None`` clears the marker. Otherwise we look up the named
    state on ``spec`` to extract its ``allowed_tools`` and record both
    on the marker so a Claude Code (or other layer-4) hook can decide
    in O(1) whether to allow a given tool call.

    Any filesystem failure here is swallowed and logged — losing the
    marker degrades enforcement but must NEVER break the run itself.
    """
    try:
        project_root = _project_root_for_marker()
        if run_id is None:
            write_active_run_marker(None, project_root=project_root)
            return
        allowed: list[str] = []
        current_state: str | None = state_id
        if spec is not None and state_id is not None:
            try:
                state = spec.get_state(state_id)
                allowed = list(state.allowed_tools or [])
            except KeyError:
                # Unknown state id — record the run + empty allowlist so
                # the hook fails open rather than blocking everything.
                allowed = []
        write_active_run_marker(
            run_id,
            project_root=project_root,
            allowed_tools=allowed,
            current_state=current_state,
        )
    except OSError:
        _LOG.exception("failed to update active-run marker")


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
            "Optional commit signature (layer-5 cosignature). When the "
            "current state declares allowed_tools, a verifier, or the "
            "server runs with CTXR_FSM_REQUIRE_COSIGNATURE=1, the "
            "signature is REQUIRED and a missing/mismatched value rejects "
            "the commit. Computed as "
            "CommitSignature.compute(brief_id, inputs, outputs, session_id)."
        ),
    )
    brief_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "Brief id the worker is committing against. Required when a "
            "signature is supplied: the signature hashes (brief_id, "
            "inputs, outputs, session_id) so verification needs the same "
            "brief_id the worker saw."
        ),
    )
    session_id: str | None = Field(
        default=None,
        description=(
            "Worker session id that signed the commit. Required when a "
            "signature is supplied."
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

    W12 two-phase semantics
    -----------------------

    When ``kind`` is ``"advanced"``, ``"loop_continued"``, or
    ``"terminal"``, the result carries a :class:`CommitToken` plus
    ``expected_next_state``. The state-row + transition-row + manifest
    update are NOT applied yet — they are staged in a ``journal_txn``
    row marked ``ready_to_finalise`` and replayed by
    :func:`fsm_confirm_commit` when the client presents the token.

    Tokens are minted only after every prior W12 gate has passed:
    signature verification, engine validation, post-validations,
    transition resolution, and (when ``State.verifier`` is set) the
    verifier panel. A ``"fault"`` or rejected-verifier result returns
    ``token=None`` and the run does not advance.
    """

    model_config = _OUT_CFG

    kind: CommitResultKind
    brief: Brief | None = None
    next_state: str | None = None
    iteration_n: int | None = None
    verdict: Any = None
    reason: str | None = None
    errors: list[str] = Field(default_factory=list)
    evaluations: list[TransitionEvaluation] = Field(default_factory=list)
    post_validations: list[PostValidationResultEntry] = Field(default_factory=list)
    token: CommitToken | None = None
    expected_next_state: str | None = None


class ConfirmCommitInput(BaseModel):
    """Arguments for :func:`fsm_confirm_commit`."""

    model_config = _IN_CFG

    token: uuid.UUID
    expected_next_state: str


class ConfirmResult(BaseModel):
    """Return value of :func:`fsm_confirm_commit`.

    W12 two-phase commit
    --------------------

    ``confirmed=True`` iff the token existed, was unconsumed, had not
    expired, and the supplied ``expected_next_state`` matched the value
    the token was minted against. On success the staged journal_txn is
    replayed (state-row + transition-row + manifest update + lifecycle
    events) and the response carries the newly-minted ``next_brief``
    plus a fresh ``manifest`` snapshot.

    ``next_brief`` is populated for ``advanced`` / ``loop_continued``
    transitions; for ``terminal`` it stays ``None`` because there is no
    further state to brief on.
    """

    model_config = _OUT_CFG

    confirmed: bool
    note: str | None = None
    next_brief: Brief | None = None
    manifest: dict[str, Any] | None = None


class ResumeRunInput(BaseModel):
    """Arguments for :func:`fsm_resume_run`."""

    model_config = _IN_CFG

    run_id: uuid.UUID
    from_state: str | None = None
    journal: JournalAction | None = None


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
# Enforcement constants (W12)
# ---------------------------------------------------------------------------


# Env-var gate for the layer-5 commit cosignature requirement. When set
# to "1" (the literal string), every ``fsm.commit_outputs`` call MUST
# carry a valid ``signature``; missing signatures surface as
# ``signature_required`` and mismatched signatures as
# ``signature_mismatch``. The cosignature is also required, regardless
# of the env var, when the current state declares ``allowed_tools`` or a
# ``verifier`` (those are stronger trust contracts and the cosignature
# is the proof that the brief and the committed outputs match).
_COSIGNATURE_ENV_VAR: str = "CTXR_FSM_REQUIRE_COSIGNATURE"


def _cosignature_required(state: Any) -> bool:
    """Return ``True`` when the W12 layer-5 cosignature must be present.

    Three triggers escalate a state's commit to "signature required":

    * The process-wide env var :data:`_COSIGNATURE_ENV_VAR` is set to
      ``"1"`` (operator opts the whole server into strict mode).
    * The state declares any ``allowed_tools`` entry — once we hand a
      worker a capability surface, the cosignature is the proof that
      the outputs came from the brief we wrote.
    * The state declares a ``verifier`` — verifier panels run against
      the committed outputs, and a signature mismatch must reject the
      commit before any verifier work begins.
    """
    if os.environ.get(_COSIGNATURE_ENV_VAR) == "1":
        return True
    allowed_tools = getattr(state, "allowed_tools", None) or []
    if allowed_tools:
        return True
    return getattr(state, "verifier", None) is not None


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
    in-process. Returns ``None`` when the spec row is missing. The
    :func:`ctxr.fsm.core.spec.attach_methods` shim is imported here so
    ``spec.hash()`` is always callable on the returned object (the spec
    module installs the method on first import; the import below is the
    safe entry point even when this module is loaded before any other
    caller has touched ``ctxr.fsm.core.spec``).
    """
    # Side-effect import: binds ``FsmSpec.hash()`` / ``FsmSpec.validate()``.
    # Idempotent and cheap once the module is on the path.
    from ctxr.fsm.core import spec as _spec_module  # noqa: F401

    with project.session_factory() as session:
        registered = project.specs.get(session, registered_spec_id)
    if registered is None:
        return None
    return FsmSpec.model_validate(registered.definition)


def _spec_hash_lock_error(
    run_hash: str,
    current_hash: str,
) -> McpToolError:
    """Construct the canonical ``fsm_spec_changed`` error envelope.

    Centralised so the MCP and (indirectly) API layers emit the exact
    same payload — clients can branch on ``error == "fsm_spec_changed"``
    and reach for ``payload.run_hash`` / ``payload.current_hash`` to
    show the operator the drift. The detail string is fixed so log
    grepping is reliable.
    """
    return as_error(
        "fsm_spec_changed",
        detail="FSM spec hash changed since run started",
        run_hash=run_hash,
        current_hash=current_hash,
    )


def _current_spec_hash_for_run(project: Project, run_spec: Any) -> str:
    """Return the hash of the *latest* registered version for the run's slug.

    The W12 spec-hash lock is about "the spec the operator currently
    considers canonical for this slug" — re-registering a v2 under the
    same slug must trip the lock, even though the run still references
    the v1 row PK. We therefore resolve the latest version under
    ``(project_id, slug)`` and use its hash for the comparison.

    Falls back to the spec's own hash when no version is registered
    under that slug (defensive — should not happen for a run that was
    started against a registered spec, but the fallback keeps the
    function total).
    """
    with project.session_factory() as session:
        versions = project.specs.list_versions(
            session,
            project_id=run_spec.project_id,
            slug=run_spec.slug,
        )
    if not versions:
        return str(run_spec.hash)
    # ``list_versions`` returns oldest-first; the last entry is the
    # newest registered version — the one the lock compares against.
    return str(versions[-1].hash)


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


def _record_verified_signature(
    project: Project,
    *,
    run_id: str,
    state_pk: str,
    state_id: str,
    iteration_n: int | None,
    envelope: CommitSignature,
    producer_id: str,
) -> None:
    """Persist a verified :class:`CommitSignature` + emit the verified event.

    Called after the cosignature check passes and the engine has decided
    the commit will proceed (not faulted). Wrapped in one atomic block
    so the audit-trail row and the bus event land together — partial
    visibility would let a subscriber see the event without the
    underlying envelope row, which would corrupt downstream replay.
    """
    with project.session_factory() as session, session.begin():
        project.commit_signatures.record(
            session,
            run_id=run_id,
            state_pk=state_pk,
            iteration_n=iteration_n,
            brief_id=str(envelope.brief_id),
            inputs_hash=envelope.inputs_hash,
            outputs_hash=envelope.outputs_hash,
            session_id=envelope.session_id,
            signature=envelope.signature,
            verified=True,
        )
        project.events.emit(
            session,
            producer_id=producer_id,
            kind=EventKind.commit_signature_verified.value,
            payload={
                "run_id": run_id,
                "state": state_id,
                "brief_id": str(envelope.brief_id),
                "signature": envelope.signature,
                "iteration_n": iteration_n,
            },
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
@drain_aware
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

        # W12 layer-4: publish the active-run marker so any installed
        # Claude Code (or peer) tool-use hook can constrain the worker
        # to the entry state's allowed_tools.
        _publish_active_run_marker(
            run_id=run.id,
            spec=spec,
            state_id=entry_state.id,
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
@drain_aware
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

        # Load the registered spec row directly so we have both
        # ``project_id`` (for the lock check below) and the canonical
        # JSON definition (to reconstruct the FsmSpec the engine wants).
        with project.session_factory() as session:
            registered = project.specs.get(session, run.fsm_spec_id)
        if registered is None:
            return as_error(
                "spec_not_found",
                detail=f"run references missing spec {run.fsm_spec_id!r}",
                spec_id=run.fsm_spec_id,
            )
        # Side-effect import binds ``FsmSpec.hash`` / ``.validate``.
        from ctxr.fsm.core import spec as _spec_module  # noqa: F401
        spec = FsmSpec.model_validate(registered.definition)

        # W12 layer-9: spec-hash lock. Compare the run's snapshot hash
        # against the *latest* registered version under the same slug
        # — re-registering a new shape under the same name must trip
        # the lock even when the run still references the original
        # row's PK.
        current_hash = _current_spec_hash_for_run(project, registered)
        if run.fsm_spec_hash != current_hash:
            return _spec_hash_lock_error(
                run_hash=run.fsm_spec_hash,
                current_hash=current_hash,
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


# ---------------------------------------------------------------------------
# Two-phase commit (W12) — staged journal txn + token issue / replay
# ---------------------------------------------------------------------------


# The token TTL is short enough that an unattended worker cannot
# permanently hold open a half-commit, long enough that an LLM client
# has time to make the follow-up confirm_commit call. The brief calls
# out 60s explicitly; we surface it as a module constant so future
# tuning happens in one place.
_COMMIT_TOKEN_TTL_SECONDS: int = 60


def _stage_commit_writes(
    *,
    result_kind: EngineAdvanceKind,
    run_id: str,
    spec_id: str,
    current_state_id: str,
    next_state_id: str | None,
    from_state_pk: str | None,
    outputs: dict[str, Any],
    env: dict[str, Any],
    iteration_n: int | None,
    verdict: Any,
    winning_kind: str | None,
    winning_predicate: str | None,
    winning_predicate_result: bool | None,
    next_inputs: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Build the canonical ``staged_writes`` payload for a journal_txn.

    The list is a flat sequence of step dicts; :func:`_replay_journal_txn`
    walks it in order to materialise the deferred state-row +
    transition-row + manifest update on confirm. Each step is a plain
    JSON-serialisable dict so the row is byte-stable across the
    canonical encoder used by ``JournalRepo.mark_ready``.
    """
    steps: list[dict[str, Any]] = []

    exits_current_state = result_kind in {
        EngineAdvanceKind.advance,
        EngineAdvanceKind.terminal,
    }
    if exits_current_state and from_state_pk is not None:
        # The exiting state — its outputs land on the state-entry row,
        # the run's last_update_at bumps, and a ``state_exited`` event
        # is emitted by the replay so subscribers see the same shape
        # they did under single-phase commit.
        steps.append(
            {
                "op": "mark_state_exited",
                "state_pk": from_state_pk,
                "outputs": dict(outputs),
                "state_id": current_state_id,
            }
        )

    if result_kind is EngineAdvanceKind.advance:
        steps.append(
            {
                "op": "record_transition",
                "from_state_pk": from_state_pk,
                "to_state_id": next_state_id or "",
                "kind": winning_kind or TransitionKind.always.value,
                "predicate": winning_predicate,
                "predicate_result": winning_predicate_result,
            }
        )
        steps.append(
            {
                "op": "persist_state_entry",
                "state_id": next_state_id or "",
                "inputs": dict(next_inputs or {}),
            }
        )

    if result_kind is EngineAdvanceKind.loop_continue:
        # The state-entry row stays open across iterations; nothing to
        # mark exited. We still stage a no-op step so replay produces a
        # deterministic event trail.
        steps.append(
            {
                "op": "loop_continue",
                "state_id": current_state_id,
                "iteration_n": iteration_n,
            }
        )

    if result_kind is EngineAdvanceKind.terminal:
        steps.append(
            {
                "op": "complete_run",
                "verdict": (str(verdict) if verdict is not None else None),
            }
        )

    # Carry the env-merged-with-outputs snapshot so confirm can rebuild
    # the next brief without re-running the engine pipeline. The shape
    # is canonical-JSON-friendly because every value is already a
    # Python primitive (env / outputs are dicts of JSON-able values by
    # contract).
    # ``result_kind`` is serialised as its string value (StrEnum
    # collapses to str under json) so the wire payload stays identical
    # to the pre-W14i shape.
    steps.append(
        {
            "op": "_meta",
            "spec_id": spec_id,
            "run_id": run_id,
            "result_kind": result_kind.value,
            "current_state_id": current_state_id,
            "next_state_id": next_state_id,
            "env_after": {**env, **outputs},
        }
    )

    return steps


def _replay_journal_txn(
    project: Project,
    *,
    spec: FsmSpec,
    run_id: str,
    producer_id: str,
    staged_writes: list[dict[str, Any]],
) -> dict[str, Any]:
    """Apply the staged writes against the substrate and emit lifecycle events.

    Returns a small dict carrying the names the caller needs for the
    confirm response: ``next_state_id`` (if any), ``next_brief`` (if
    any), ``result_kind`` (so the caller can branch on terminal /
    advance / loop_continue).
    """
    # Pull the meta envelope first — we need ``result_kind`` /
    # ``next_state_id`` before we walk the ops so the replay can keep
    # the right invariants (e.g. terminal must NOT persist a next
    # state-entry).
    meta: dict[str, Any] = next(
        (step for step in staged_writes if step.get("op") == "_meta"), {}
    )
    result_kind: str = str(meta.get("result_kind", ""))
    next_state_id: str | None = meta.get("next_state_id")
    current_state_id: str = str(meta.get("current_state_id", ""))
    env_after: dict[str, Any] = dict(meta.get("env_after") or {})

    next_state_entry_pk: str | None = None

    for step in staged_writes:
        op = step.get("op")
        if op == "mark_state_exited":
            state_pk = str(step["state_pk"])
            _record_state_exit(
                project,
                run_id=run_id,
                state_pk=state_pk,
                outputs=dict(step.get("outputs") or {}),
                producer_id=producer_id,
            )
        elif op == "record_transition":
            from_pk = step.get("from_state_pk")
            if from_pk is None:
                continue
            _record_transition(
                project,
                run_id=run_id,
                from_state_pk=str(from_pk),
                to_state_id=str(step.get("to_state_id") or ""),
                kind=str(step.get("kind") or TransitionKind.always.value),
                predicate=step.get("predicate"),
                predicate_result=step.get("predicate_result"),
                producer_id=producer_id,
            )
        elif op == "persist_state_entry":
            next_state_entry_pk = _persist_state_entry(
                project,
                run_id=run_id,
                state_id=str(step.get("state_id") or ""),
                inputs=dict(step.get("inputs") or {}),
                producer_id=producer_id,
            )
        elif op == "complete_run":
            now = _iso_now_ms()
            verdict = step.get("verdict")
            with project.session_factory() as session, session.begin():
                project.runs.update_status(
                    session,
                    run_id=run_id,
                    status="completed",
                    ended_at=now,
                    verdict=verdict,
                )
                project.events.emit(
                    session,
                    producer_id=producer_id,
                    kind=EventKind.run_completed.value,
                    payload={
                        "run_id": run_id,
                        "verdict": verdict,
                        "ended_at": now,
                    },
                    run_id=run_id,
                )
        elif op == "loop_continue":
            # No state-row mutation; loop replay is a no-op as far as
            # the substrate is concerned. The iteration's brief is
            # rebuilt below from spec + env_after.
            continue
        # ``_meta`` is consumed up front; anything unknown is ignored
        # so a forward-compatible producer can stage richer payloads
        # without breaking older replayers.

    next_brief: Brief | None = None
    if result_kind == EngineAdvanceKind.advance.value and next_state_id:
        next_state_obj = spec.get_state(next_state_id)
        next_brief = build_brief(
            spec,
            next_state_obj,
            env=env_after,
            run_id=uuid.UUID(run_id),
        )
    elif result_kind == EngineAdvanceKind.loop_continue.value:
        # The loop body's next brief is already in the CommitResult
        # held by the worker; we still rebuild it here so confirm can
        # echo it back uniformly.
        loop_state = spec.get_state(current_state_id)
        iter_n = meta.get("iteration_n")
        next_brief = build_brief(
            spec,
            loop_state,
            env=env_after,
            run_id=uuid.UUID(run_id),
            iteration_n=iter_n if isinstance(iter_n, int) else None,
        )

    return {
        "result_kind": result_kind,
        "next_state_id": next_state_id,
        "next_brief": next_brief,
        "next_state_entry_pk": next_state_entry_pk,
    }


def _manifest_for_run(project: Project, run_id: str) -> dict[str, Any] | None:
    """Return a JSON-able manifest snapshot for ``run_id`` (or ``None``)."""
    run = project.get_run(run_id)
    if run is None:
        return None
    return run.model_dump(mode="json")


@mcp.tool(
    name="fsm.commit_outputs",
    description=(
        "Commit worker outputs for the run's current state. W12 two-phase: "
        "drives the engine to validate + decide loop/transition/terminal, "
        "runs the verifier panel when the state declares one, stages the "
        "resulting writes in a journal_txn marked ready_to_finalise, and "
        "returns a single-use CommitToken. The client must call "
        "fsm.confirm_commit with the token to actually advance the run."
    ),
)
@drain_aware
def fsm_commit_outputs(input: CommitOutputsInput) -> CommitResult | McpToolError:
    """Implement ``fsm.commit_outputs`` — W12 two-phase commit + verifier.

    Flow:

    1. Resolve the run + spec; run the layer-9 spec-hash lock.
    2. Materialise the env + run the layer-5 commit cosignature check.
    3. Drive the pure engine via :func:`engine_advance`.
    4. On fault: emit ``validation_failed``, return ``kind="fault"`` (no
       token).
    5. On non-fault: if the state declares a ``verifier``, run the
       verifier panel; on reject return ``verifier_rejected`` (no
       token); on pass emit ``verifier_passed``.
    6. Persist any verified cosignature (layer 5 audit row + event).
    7. Stage the deferred writes in a ``journal_txn`` row marked
       ``ready_to_finalise`` and mint a :class:`CommitToken` bound to
       ``(run_id, current_state, expected_next_state)`` with the
       module-default TTL.
    8. Return :class:`CommitResult` carrying the brief, token, and
       ``expected_next_state``. The state-entry / transition / manifest
       updates land on :func:`fsm_confirm_commit`.
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

        # Load the registered spec row directly so we have both
        # ``project_id`` / ``slug`` (for the W12 lock check) and the
        # canonical JSON definition (to rebuild the FsmSpec the engine
        # operates against). The run stays bound to the *original*
        # spec version for FSM semantics; the lock check is what
        # surfaces the drift to the operator.
        with project.session_factory() as session:
            registered = project.specs.get(session, run.fsm_spec_id)
        if registered is None:
            return as_error(
                "spec_not_found",
                detail=f"run references missing spec {run.fsm_spec_id!r}",
                spec_id=run.fsm_spec_id,
            )
        from ctxr.fsm.core import spec as _spec_module  # noqa: F401
        spec = FsmSpec.model_validate(registered.definition)

        # W12 layer-9: spec-hash lock. Compare the run's snapshot hash
        # against the *latest* registered version under the same slug
        # — re-registering a new shape under the same name must trip
        # the lock even when the run still references the original
        # row's PK.
        current_hash = _current_spec_hash_for_run(project, registered)
        if run.fsm_spec_hash != current_hash:
            return _spec_hash_lock_error(
                run_hash=run.fsm_spec_hash,
                current_hash=current_hash,
            )

        current_state_id = _current_state_id(run, spec)
        try:
            current_state = spec.get_state(current_state_id)
        except KeyError:
            return as_error(
                "invalid_state",
                detail=(
                    f"run.current_state {current_state_id!r} is not present "
                    "in spec.states"
                ),
                state=current_state_id,
            )

        # Materialise the run env *before* the cosignature check —
        # signature verification hashes inputs (= env merged with the
        # run's seed args) so we need the env in hand to call
        # CommitSignature.compute.
        env = _materialise_env(project, run)

        # W12 layer-5: commit cosignature. Triggered when the env-var
        # opts the server into strict mode, or when the state itself
        # declares allowed_tools / a verifier. The producer_id we need
        # for the verified/mismatched events is the same engine producer
        # the rest of this body uses; mint it up front so the early
        # error branches can attribute their events correctly.
        producer_id = _ensure_engine_producer(project)
        signature_required = _cosignature_required(current_state)
        signature_verified = False
        signature_envelope: CommitSignature | None = None

        if input.signature is not None:
            # Verification path. Both brief_id and session_id MUST be
            # supplied so the hash inputs are unambiguous; reject up
            # front rather than silently using placeholder zeros that
            # could never match a real worker-side compute.
            if input.brief_id is None or input.session_id is None:
                return as_error(
                    "signature_required",
                    detail=(
                        "brief_id and session_id are required when a "
                        "signature is supplied"
                    ),
                )
            signature_envelope = CommitSignature.compute(
                brief_id=input.brief_id,
                inputs=dict(env),
                outputs=dict(input.outputs),
                session_id=input.session_id,
            )
            if signature_envelope.signature != input.signature:
                # Emit the mismatch event for the drift aggregator
                # before returning so the audit trail captures the
                # rejection regardless of how the caller handles the
                # error envelope.
                with project.session_factory() as session, session.begin():
                    project.events.emit(
                        session,
                        producer_id=producer_id,
                        kind=EventKind.commit_signature_mismatch.value,
                        payload={
                            "run_id": run.id,
                            "state": current_state_id,
                            "brief_id": str(input.brief_id),
                            "expected": signature_envelope.signature,
                            "got": input.signature,
                        },
                        run_id=run.id,
                    )
                return as_error(
                    "signature_mismatch",
                    detail="commit signature does not match expected value",
                    expected=signature_envelope.signature,
                    got=input.signature,
                )
            signature_verified = True
        elif signature_required:
            # Required-but-missing path. We do NOT emit a drift signal
            # here yet — the call has not produced any outputs to bind,
            # so there is nothing to redact / persist; the caller is
            # expected to retry with the signature attached.
            return as_error(
                "signature_required",
                detail=(
                    "this state requires a commit signature "
                    "(allowed_tools / verifier / CTXR_FSM_REQUIRE_COSIGNATURE=1)"
                ),
                state=current_state_id,
            )

        ctx = RunCtx(
            run_id=uuid.UUID(run.id),
            fsm_id=spec.id,
            current_state=current_state_id,
            env=env,
        )

        result = engine_advance(spec, ctx, dict(input.outputs))
        from_pk = _current_state_pk(project, run.id, current_state_id)

        if result.kind == "fault":
            # Audit-trail the failure so subscribers see it on the bus;
            # do NOT mark the state exited, mint a token, or stage a
            # journal txn — the worker is expected to retry or the
            # operator to intervene.
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
                kind=CommitResultKind.fault,
                reason=result.reason,
                errors=list(result.errors),
                evaluations=list(result.evaluations),
                post_validations=list(result.post_validations),
            )

        # ── W12 layer-3: adversarial verifier panel ──────────────────
        # Runs AFTER engine.advance produces a non-fault outcome but
        # BEFORE token issuance. A rejected panel surfaces as
        # ``verifier_rejected`` and does NOT stage a journal txn or mint
        # a token — the run sits on the current state until the worker
        # retries with a passing payload.
        if current_state.verifier is not None:
            # ``result.brief`` for advance/loop_continue is the *next*
            # brief; we want the brief the worker just committed
            # against. Rebuild it from the current state + env.
            iter_for_brief = (
                getattr(result, "iteration_n", None)
                if result.kind is EngineAdvanceKind.loop_continue
                else None
            )
            current_brief = build_brief(
                spec,
                current_state,
                env=env,
                run_id=uuid.UUID(run.id),
                iteration_n=iter_for_brief,
            )
            verifier_outcome: VerifierOutcome = run_verifier(
                current_state.verifier, current_brief, dict(input.outputs)
            )

            with project.session_factory() as session, session.begin():
                project.events.emit(
                    session,
                    producer_id=producer_id,
                    kind=(
                        EventKind.verifier_passed.value
                        if verifier_outcome.verdict is VerifierVerdict.passed
                        else EventKind.verifier_rejected.value
                    ),
                    payload={
                        "run_id": run.id,
                        "state": current_state_id,
                        "verdict": verifier_outcome.verdict,
                        "passed_count": verifier_outcome.passed_count,
                        "rejected_count": verifier_outcome.rejected_count,
                        "majority_threshold": verifier_outcome.majority_threshold,
                        "parallel_count": verifier_outcome.parallel_count,
                        "votes": [
                            vote.model_dump(mode="json")
                            for vote in verifier_outcome.votes
                        ],
                    },
                    run_id=run.id,
                )

            if verifier_outcome.verdict is VerifierVerdict.rejected:
                return as_error(
                    "verifier_rejected",
                    detail="verifier panel rejected the worker outputs",
                    state=current_state_id,
                    verdicts=[
                        vote.model_dump(mode="json")
                        for vote in verifier_outcome.votes
                    ],
                    reasons=[vote.reason for vote in verifier_outcome.votes],
                    passed_count=verifier_outcome.passed_count,
                    rejected_count=verifier_outcome.rejected_count,
                    majority_threshold=verifier_outcome.majority_threshold,
                )

        # Persist any verified cosignature now — it binds the brief +
        # outputs at commit time, independent of whether the journal
        # txn is later finalised by confirm_commit. The audit trail
        # captures "the worker signed this commit" even if the operator
        # never confirms (in which case the staged writes are simply
        # discarded at reaper time).
        if signature_verified and signature_envelope is not None and from_pk is not None:
            _record_verified_signature(
                project,
                run_id=run.id,
                state_pk=from_pk,
                state_id=current_state_id,
                iteration_n=getattr(result, "iteration_n", None),
                envelope=signature_envelope,
                producer_id=producer_id,
            )

        # ── Stage writes + mint token (deferred to confirm_commit) ──
        if result.kind is EngineAdvanceKind.loop_continue:
            staged = _stage_commit_writes(
                result_kind=EngineAdvanceKind.loop_continue,
                run_id=run.id,
                spec_id=spec.id,
                current_state_id=current_state_id,
                next_state_id=current_state_id,
                from_state_pk=from_pk,
                outputs=dict(input.outputs),
                env=env,
                iteration_n=result.iteration_n,
                verdict=None,
                winning_kind=None,
                winning_predicate=None,
                winning_predicate_result=None,
                next_inputs=None,
            )
            token = _open_and_stage_journal(
                project,
                run_id=run.id,
                current_state_id=current_state_id,
                expected_next_state=current_state_id,
                staged_writes=staged,
            )
            return CommitResult(
                kind=CommitResultKind.loop_continued,
                brief=result.brief,
                iteration_n=result.iteration_n,
                token=_to_wire_token(token),
                expected_next_state=current_state_id,
            )

        if result.kind is EngineAdvanceKind.terminal:
            staged = _stage_commit_writes(
                result_kind=EngineAdvanceKind.terminal,
                run_id=run.id,
                spec_id=spec.id,
                current_state_id=current_state_id,
                next_state_id=None,
                from_state_pk=from_pk,
                outputs=dict(input.outputs),
                env=env,
                iteration_n=None,
                verdict=result.verdict,
                winning_kind=None,
                winning_predicate=None,
                winning_predicate_result=None,
                next_inputs=None,
            )
            # ``expected_next_state`` for a terminal commit is the
            # sentinel ``"__terminal__"`` — there is no actual next
            # state, but the confirm-side code needs a non-empty string
            # to compare against. The token records the same sentinel.
            terminal_marker = "__terminal__"
            token = _open_and_stage_journal(
                project,
                run_id=run.id,
                current_state_id=current_state_id,
                expected_next_state=terminal_marker,
                staged_writes=staged,
            )
            return CommitResult(
                kind=CommitResultKind.terminal,
                verdict=result.verdict,
                evaluations=list(result.evaluations),
                token=_to_wire_token(token),
                expected_next_state=terminal_marker,
            )

        # result.kind == "advance"
        # Find the winning transition's guard kind / predicate text from
        # the evaluations trace so the staged transition row is faithful.
        winning_eval: TransitionEvaluation | None = None
        for ev in result.evaluations:
            if ev.result and ev.to == result.next_state:
                winning_eval = ev
                break

        next_state_id = result.next_state or ""
        next_state = spec.get_state(next_state_id)
        next_worker = next_state.worker or (
            next_state.loop.worker if next_state.loop is not None else None
        )
        merged_env = {**env, **dict(input.outputs)}
        next_inputs: dict[str, Any] = {}
        if next_worker is not None:
            next_inputs = {name: merged_env.get(name) for name in next_worker.inputs}

        unconditional_kinds = {
            TransitionKind.always.value,
            TransitionKind.otherwise.value,
        }
        staged = _stage_commit_writes(
            result_kind=EngineAdvanceKind.advance,
            run_id=run.id,
            spec_id=spec.id,
            current_state_id=current_state_id,
            next_state_id=next_state_id,
            from_state_pk=from_pk,
            outputs=dict(input.outputs),
            env=env,
            iteration_n=None,
            verdict=None,
            winning_kind=(
                (winning_eval.kind if winning_eval else None)
                or TransitionKind.always.value
            ),
            winning_predicate=(
                winning_eval.expression if winning_eval else None
            ),
            winning_predicate_result=(
                None
                if winning_eval is None
                or winning_eval.kind in unconditional_kinds
                else bool(winning_eval.result)
            ),
            next_inputs=next_inputs,
        )
        token = _open_and_stage_journal(
            project,
            run_id=run.id,
            current_state_id=current_state_id,
            expected_next_state=next_state_id,
            staged_writes=staged,
        )
        return CommitResult(
            kind=CommitResultKind.advanced,
            brief=result.brief,
            next_state=result.next_state,
            evaluations=list(result.evaluations),
            token=_to_wire_token(token),
            expected_next_state=next_state_id,
        )
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        _LOG.exception("fsm.commit_outputs failed")
        return as_error("internal_error", detail=str(exc))


def _open_and_stage_journal(
    project: Project,
    *,
    run_id: str,
    current_state_id: str,
    expected_next_state: str,
    staged_writes: list[dict[str, Any]],
) -> Any:
    """Open a journal_txn, mark it ready_to_finalise, mint + persist a token.

    Returns the persisted :class:`CommitTokenRecord` (with the run-side
    UUID + expiry already populated). All three writes happen inside a
    single ``session.begin()`` so a crash between any two of them
    leaves the substrate consistent — either the operator sees a
    token + matching journal row, or neither.
    """
    with project.session_factory() as session, session.begin():
        txn = project.journal.open(session, run_id=run_id)
        # Inject the journal txn id into the meta step so confirm can
        # cross-reference and finalise the exact row.
        enriched_writes = list(staged_writes)
        for step in enriched_writes:
            if step.get("op") == "_meta":
                step["journal_txn_id"] = txn.id
        project.journal.mark_ready(
            session, txn_id=txn.id, staged_writes=enriched_writes
        )

        token_record = project.commit_tokens.issue(
            session,
            run_id=run_id,
            state_id=current_state_id,
            expected_next_state=expected_next_state,
            ttl_seconds=_COMMIT_TOKEN_TTL_SECONDS,
        )

        # Emit the commit_token_issued event so subscribers see the
        # pending hand-off. Producer is the engine itself; using
        # ``project.events.emit`` keeps the per-run seq monotonic.
        producer = project.producers.upsert(
            session, kind=_ENGINE_PRODUCER_KIND, name=_ENGINE_PRODUCER_NAME
        )
        project.events.emit(
            session,
            producer_id=producer.id,
            kind=EventKind.commit_token_issued.value,
            payload={
                "run_id": run_id,
                "token": token_record.token,
                "state_id": current_state_id,
                "expected_next_state": expected_next_state,
                "expires_at": token_record.expires_at,
                "journal_txn_id": txn.id,
            },
            run_id=run_id,
        )

    return token_record


def _to_wire_token(token_record: Any) -> CommitToken:
    """Project a persistence-side :class:`CommitTokenRecord` to the wire shape.

    The wire :class:`CommitToken` carries the same identifiers but uses
    ``uuid.UUID`` / ``datetime`` types instead of the storage-friendly
    strings. We do the conversion in one place so the call sites stay
    declarative.
    """
    expires_at = token_record.expires_at
    if isinstance(expires_at, str):
        # Persisted as the canonical ISO/Z form; tolerate the trailing Z.
        iso = expires_at[:-1] + "+00:00" if expires_at.endswith("Z") else expires_at
        expires_at_dt = datetime.fromisoformat(iso)
    else:
        expires_at_dt = expires_at
    return CommitToken(
        token=uuid.UUID(token_record.token),
        run_id=uuid.UUID(token_record.run_id),
        state_id=token_record.state_id,
        expected_next_state=token_record.expected_next_state,
        expires_at=expires_at_dt,
    )


@mcp.tool(
    name="fsm.confirm_commit",
    description=(
        "Confirm a previously-issued CommitToken: validate, consume, and "
        "replay the staged journal_txn so the run actually advances. "
        "Returns the new manifest and the next brief (if any)."
    ),
)
@drain_aware
def fsm_confirm_commit(input: ConfirmCommitInput) -> ConfirmResult | McpToolError:
    """Implement ``fsm.confirm_commit`` — finalise a two-phase commit (W12).

    Steps:
    1. Consume the token (refuse if missing / consumed / expired /
       state-mismatch).
    2. Locate the matching ``journal_txn`` row (ready_to_finalise).
    3. Replay the staged writes against the substrate.
    4. Mark the journal txn finalised.
    5. Emit ``commit_token_consumed``.
    6. Return :class:`ConfirmResult` with the next brief (if any) and a
       fresh manifest snapshot.
    """
    try:
        project = get_project()
        token_str = str(input.token)

        # 1. Validate + consume the token atomically. ``consume``
        #    returns ok=False with a discriminator slug we surface in
        #    the error envelope so the client can branch on it.
        with project.session_factory() as session, session.begin():
            consume_result = project.commit_tokens.consume(
                session,
                token=token_str,
                expected_next_state=input.expected_next_state,
            )
        if not consume_result.ok:
            # An expired token is operationally interesting — a drift
            # aggregator wants to know that a worker tried to finalise
            # too late so we surface a ``commit_token_expired`` event on
            # the bus, attributed to the token's run, before returning
            # the rejection envelope.
            if (
                consume_result.reason == "expired"
                and consume_result.token is not None
            ):
                expired_run_id = consume_result.token.run_id
                producer_id = _ensure_engine_producer(project)
                with project.session_factory() as session, session.begin():
                    project.events.emit(
                        session,
                        producer_id=producer_id,
                        kind=EventKind.commit_token_expired.value,
                        payload={
                            "run_id": expired_run_id,
                            "token": token_str,
                            "expected_next_state": (
                                consume_result.token.expected_next_state
                            ),
                            "state_id": consume_result.token.state_id,
                        },
                        run_id=expired_run_id,
                    )
            return as_error(
                "commit_token_invalid",
                detail=(
                    f"commit token cannot be consumed: {consume_result.reason!r}"
                ),
                reason=consume_result.reason,
                token=token_str,
            )

        # The consumed token carries the run_id we need to look up the
        # matching journal_txn and the spec for replay.
        consumed_token = consume_result.token
        assert consumed_token is not None  # ok=True path always populates token
        run_id = consumed_token.run_id

        run = project.get_run(run_id)
        if run is None:
            return as_error(
                "run_not_found",
                detail=f"token references missing run {run_id!r}",
                run_id=run_id,
            )

        # Reload the spec so we can rebuild the next brief during replay.
        with project.session_factory() as session:
            registered = project.specs.get(session, run.fsm_spec_id)
        if registered is None:
            return as_error(
                "spec_not_found",
                detail=f"run references missing spec {run.fsm_spec_id!r}",
                spec_id=run.fsm_spec_id,
            )
        from ctxr.fsm.core import spec as _spec_module  # noqa: F401
        spec = FsmSpec.model_validate(registered.definition)

        # 2. Find the open journal_txn. ``inspect`` returns the newest
        #    unfinalised row; under normal flow this is the one our
        #    token was minted alongside.
        with project.session_factory() as session:
            txn = project.journal.inspect(session, run_id=run_id)
        if txn is None or txn.status != "ready_to_finalise":
            return as_error(
                "journal_not_ready",
                detail=(
                    "no ready_to_finalise journal_txn found for the token's run"
                ),
                run_id=run_id,
                journal_status=(txn.status if txn is not None else None),
            )

        producer_id = _ensure_engine_producer(project)

        # 3. Replay the staged writes.
        replay = _replay_journal_txn(
            project,
            spec=spec,
            run_id=run_id,
            producer_id=producer_id,
            staged_writes=list(txn.staged_writes),
        )

        # 4. Mark the journal finalised + emit consumed/finalised events.
        with project.session_factory() as session, session.begin():
            project.journal.finalise(session, txn_id=txn.id)
            project.events.emit(
                session,
                producer_id=producer_id,
                kind=EventKind.journal_finalised.value,
                payload={
                    "run_id": run_id,
                    "journal_txn_id": txn.id,
                    "result_kind": replay.get("result_kind"),
                },
                run_id=run_id,
            )
            project.events.emit(
                session,
                producer_id=producer_id,
                kind=EventKind.commit_token_consumed.value,
                payload={
                    "run_id": run_id,
                    "token": consumed_token.token,
                    "expected_next_state": consumed_token.expected_next_state,
                },
                run_id=run_id,
            )

        # W12 layer-4: keep the active-run marker in sync with the new
        # current state. Terminal commits clear the marker so any
        # subsequent tool calls are unconstrained (no run is active).
        result_kind = replay.get("result_kind")
        if result_kind == EngineAdvanceKind.terminal.value:
            _publish_active_run_marker(run_id=None, spec=None, state_id=None)
        else:
            next_state_id = replay.get("next_state_id") or consumed_token.state_id
            _publish_active_run_marker(
                run_id=run_id, spec=spec, state_id=next_state_id
            )

        manifest = _manifest_for_run(project, run_id)
        return ConfirmResult(
            confirmed=True,
            next_brief=replay.get("next_brief"),
            manifest=manifest,
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
@drain_aware
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
@drain_aware
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

        # W12 layer-4: clear the active-run marker so a peer tool-use
        # hook stops constraining tool calls now that the run is over.
        _publish_active_run_marker(run_id=None, spec=None, state_id=None)

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
@drain_aware
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
@drain_aware
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
