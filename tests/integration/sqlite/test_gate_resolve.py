"""End-to-end integration tests for W23g gate resolution (engine + MCP tool).

Drives a small FSM with a gate state through ``fsm.start_run`` →
``fsm.get_brief`` (sees the gate) → ``fsm.resolve_gate`` → next brief.

Coverage:

* ``llm_supplied`` source kind — operator supplies the value, engine
  validates it against the gate's ``response_schema`` and lands it in
  the env under the gate state's first declared output.
* ``run_output`` source kind — the gate pulls its value from another
  run's exited state outputs via a :class:`GateBinding`.
* ``gate_schema_mismatch`` error envelope — value fails the schema.
* ``gate_value_and_binding_conflict`` error envelope — both supplied.
* ``gate_value_or_binding_required`` error envelope — neither supplied.
"""

from __future__ import annotations

import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from ctxr.fsm.core import spec as _spec_module  # noqa: F401  (binds .hash/.validate)
from ctxr.fsm.core.models import (
    FsmSpec,
    Gate,
    GateBinding,
    GateSourceKind,
    ResponseSchema,
    State,
    Transition,
    Worker,
)
from ctxr.fsm.mcp import _state as _mcp_state
from ctxr.fsm.mcp.tools_runs import (
    GetBriefInput,
    ResolveGateInput,
    StartRunInput,
    fsm_get_brief,
    fsm_resolve_gate,
    fsm_start_run,
)
from ctxr.fsm.sqlite import Project

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def project() -> Iterator[Project]:
    """Yield a fresh migrated Project bound to the MCP module-global handle."""

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fsm.sqlite3"
        proj = Project.open(db_path, migrate=True)
        _mcp_state.set_project(proj)
        try:
            yield proj
        finally:
            _mcp_state.reset_project()
            proj.close()


_VERDICT_SCHEMA = ResponseSchema(
    schema={
        "type": "object",
        "properties": {
            "review_verdict": {"type": "string", "enum": ["GO", "NO_GO"]},
        },
        "required": ["review_verdict"],
        "additionalProperties": False,
    }
)


def _llm_supplied_spec() -> FsmSpec:
    """A 3-state FSM whose middle state is an llm_supplied gate.

    Shape: ``entry`` (worker → always) → ``await_review`` (gate, always
    → ``done``) → ``done`` (terminal).
    """

    return FsmSpec(
        id="gate_llm_demo",
        version=1,
        entry="entry",
        states=[
            State(
                id="entry",
                purpose="entry state",
                worker=Worker(
                    role="dummy",
                    prompt_template="emit something",
                ),
                transitions=[Transition(to="await_review", when="always")],
            ),
            State(
                id="await_review",
                purpose="wait for review verdict",
                gate=Gate(
                    source_kind=GateSourceKind.llm_supplied,
                    response_schema=_VERDICT_SCHEMA,
                ),
                outputs=["review_verdict"],
                transitions=[Transition(to="done", when="always")],
            ),
            State(id="done", purpose="terminal", transitions=[]),
        ],
    )


def _run_output_spec() -> FsmSpec:
    """A 3-state FSM whose middle state is a run_output gate.

    The gate has no pre-populated binding — the resolver supplies one at
    resolve time. Shape mirrors the llm spec; only the gate's
    ``source_kind`` differs.
    """

    return FsmSpec(
        id="gate_run_output_demo",
        version=1,
        entry="entry",
        states=[
            State(
                id="entry",
                purpose="entry state",
                worker=Worker(
                    role="dummy",
                    prompt_template="emit something",
                ),
                transitions=[Transition(to="await_review", when="always")],
            ),
            State(
                id="await_review",
                purpose="wait for review verdict from another run",
                gate=Gate(
                    source_kind=GateSourceKind.run_output,
                    response_schema=_VERDICT_SCHEMA,
                ),
                outputs=["review_verdict"],
                transitions=[Transition(to="done", when="always")],
            ),
            State(id="done", purpose="terminal", transitions=[]),
        ],
    )


def _upstream_spec() -> FsmSpec:
    """An upstream FSM that exits its ``qa`` state with a verdict output."""

    return FsmSpec(
        id="qa_producer",
        version=1,
        entry="qa",
        states=[
            State(
                id="qa",
                purpose="produce a verdict",
                worker=Worker(
                    role="qa",
                    prompt_template="produce verdict",
                ),
                transitions=[Transition(to="done", when="always")],
            ),
            State(id="done", purpose="terminal", transitions=[]),
        ],
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _advance_entry_to_gate(project: Project, run_id: str) -> None:
    """Manually mark the entry state exited and transition to the gate.

    The integration test focuses on gate resolution semantics, not on the
    worker-commit pipeline; this helper threads the run through the
    entry state by hand so the gate becomes the current state.
    """

    spec_registered_def = project.get_run(run_id)
    assert spec_registered_def is not None
    with project.session_factory() as session:
        entries = project.states.list_by_run(session, run_id)
    entry_pk = None
    for entry in entries:
        if entry.state_id == "entry" and entry.status == "entered":
            entry_pk = entry.id
            break
    assert entry_pk is not None
    with project.session_factory() as session, session.begin():
        project.states.mark_exited(session, entry_pk, {"done": True})
        # Reflect the transition + persist the gate state-entry row so
        # ``runs.current_state`` lands on the gate.
        project.transitions.create(
            session,
            run_id=run_id,
            from_state_pk=entry_pk,
            to_state_id="await_review",
            kind="always",
            predicate=None,
            predicate_result=None,
        )
        next_seq = project.states.next_entry_seq(session, run_id)
        project.states.create(
            session,
            run_id=run_id,
            state_id="await_review",
            inputs={},
            entry_seq=next_seq,
        )
        from ctxr.fsm.sqlite.models_core import RunTable

        run_row = session.get(RunTable, run_id)
        assert run_row is not None
        run_row.current_state = "await_review"


def _open_gate_entry_seq(project: Project, run_id: str, state_id: str) -> int:
    """Return the entry_seq of the currently-open gate state row.

    Used by integration tests so they exercise the contract's explicit
    ``state_entry_seq`` path rather than relying on the tool's
    backfill-from-open-entry fallback (a regression in the resolver's
    seq handling would silently be masked otherwise).
    """

    with project.session_factory() as session:
        entries = project.states.list_by_run(session, run_id)
    for entry in reversed(entries):
        if entry.state_id == state_id and entry.status == "entered":
            return entry.entry_seq
    raise AssertionError(
        f"no open state-entry row for {state_id!r} on run {run_id!r}"
    )


def _drive_qa_run(project: Project, *, verdict: str = "GO") -> str:
    """Start an upstream QA run and walk its ``qa`` state to exit.

    Returns the source run id with ``qa`` already exited and carrying
    ``review_verdict={verdict}`` on its outputs, ready for a downstream
    gate binding.
    """

    spec = _upstream_spec()
    registered = project.register_spec(spec)
    started = fsm_start_run(
        StartRunInput(spec_id=registered.spec.id, args={})
    )
    assert not hasattr(started, "error"), f"start_run: {started!r}"
    run_id = str(started.run_id)

    with project.session_factory() as session:
        entries = project.states.list_by_run(session, run_id)
    qa_pk = next(
        (e.id for e in entries if e.state_id == "qa" and e.status == "entered"),
        None,
    )
    assert qa_pk is not None
    with project.session_factory() as session, session.begin():
        project.states.mark_exited(session, qa_pk, {"review_verdict": verdict})
    return run_id


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_llm_supplied_gate_resolves_and_advances(project: Project) -> None:
    """start_run → get_brief sees gate → resolve_gate(value=...) → next brief."""

    spec = _llm_supplied_spec()
    registered = project.register_spec(spec)
    started = fsm_start_run(StartRunInput(spec_id=registered.spec.id, args={}))
    assert not hasattr(started, "error"), f"start_run: {started!r}"
    run_id = str(started.run_id)

    # Land the run on the gate by exiting the entry state by hand.
    _advance_entry_to_gate(project, run_id)

    # get_brief now sees a gate.
    brief = fsm_get_brief(GetBriefInput(run_id=uuid.UUID(run_id)))
    assert not hasattr(brief, "error"), f"get_brief: {brief!r}"
    assert brief.gate is not None, "brief.gate must be populated for gate states"
    assert brief.has_worker is False

    # Resolve the gate with a well-shaped value. Pass the actual open
    # gate state_entry_seq so the resolver's seq handling is exercised
    # explicitly rather than masked by the tool-side backfill fallback.
    open_seq = _open_gate_entry_seq(project, run_id, "await_review")
    resolved = fsm_resolve_gate(
        ResolveGateInput(
            run_id=run_id,
            state_entry_seq=open_seq,
            value={"review_verdict": "GO"},
        )
    )
    assert not hasattr(resolved, "error"), f"resolve_gate: {resolved!r}"
    assert resolved.resolved is True
    assert resolved.env_update == {"review_verdict": "GO"}
    assert resolved.next_state == "done"

    # gate_bindings row landed and pins to the actual open gate seq.
    with project.session_factory() as session:
        records = project.gates.by_target_run(session, run_id)
    assert len(records) == 1
    assert records[0].source_kind == "llm_supplied"
    assert records[0].target_field == "review_verdict"
    assert records[0].target_state_entry_seq == open_seq


def test_run_output_gate_resolves_via_binding(project: Project) -> None:
    """A run_output gate pulls its value from another run's state output."""

    source_run_id = _drive_qa_run(project, verdict="GO")

    spec = _run_output_spec()
    registered = project.register_spec(spec)
    started = fsm_start_run(StartRunInput(spec_id=registered.spec.id, args={}))
    assert not hasattr(started, "error"), f"start_run: {started!r}"
    run_id = str(started.run_id)

    _advance_entry_to_gate(project, run_id)

    binding = GateBinding(
        source_run_id=source_run_id,
        source_state_id="qa",
        source_field="review_verdict",
        target_field="review_verdict",
    )
    open_seq = _open_gate_entry_seq(project, run_id, "await_review")
    resolved = fsm_resolve_gate(
        ResolveGateInput(
            run_id=run_id,
            state_entry_seq=open_seq,
            binding=binding.model_dump(),
        )
    )
    assert not hasattr(resolved, "error"), f"resolve_gate: {resolved!r}"
    assert resolved.env_update == {"review_verdict": "GO"}
    assert resolved.next_state == "done"

    # The binding row points at the source run; the by_source_run index
    # surfaces this run in the upstream's OUTGOING bindings.
    with project.session_factory() as session:
        outgoing = project.gates.by_source_run(session, source_run_id)
    assert len(outgoing) == 1
    assert outgoing[0].target_run_id == run_id
    assert outgoing[0].source_kind == "run_output"


# ---------------------------------------------------------------------------
# Error envelope vocabulary
# ---------------------------------------------------------------------------


def test_schema_mismatch_returns_typed_envelope(project: Project) -> None:
    """A value that fails the gate's response_schema rejects with the typed code."""

    spec = _llm_supplied_spec()
    registered = project.register_spec(spec)
    started = fsm_start_run(StartRunInput(spec_id=registered.spec.id, args={}))
    run_id = str(started.run_id)
    _advance_entry_to_gate(project, run_id)

    bad = fsm_resolve_gate(
        ResolveGateInput(
            run_id=run_id,
            state_entry_seq=0,
            # Wrong enum value; the schema requires GO / NO_GO.
            value={"review_verdict": "MAYBE"},
        )
    )
    assert hasattr(bad, "error"), f"expected error envelope, got: {bad!r}"
    assert bad.error == "gate_schema_mismatch"


def test_value_and_binding_conflict_rejects(project: Project) -> None:
    """Supplying both `value` and `binding` rejects with the typed code."""

    spec = _llm_supplied_spec()
    registered = project.register_spec(spec)
    started = fsm_start_run(StartRunInput(spec_id=registered.spec.id, args={}))
    run_id = str(started.run_id)
    _advance_entry_to_gate(project, run_id)

    binding = GateBinding(
        source_run_id=str(uuid.uuid4()),
        source_state_id="qa",
        source_field="review_verdict",
        target_field="review_verdict",
    )
    bad = fsm_resolve_gate(
        ResolveGateInput(
            run_id=run_id,
            state_entry_seq=0,
            value={"review_verdict": "GO"},
            binding=binding.model_dump(),
        )
    )
    assert hasattr(bad, "error"), f"expected error envelope, got: {bad!r}"
    assert bad.error == "gate_value_and_binding_conflict"


def test_value_or_binding_required_rejects(project: Project) -> None:
    """Supplying neither `value` nor `binding` rejects with the typed code."""

    spec = _llm_supplied_spec()
    registered = project.register_spec(spec)
    started = fsm_start_run(StartRunInput(spec_id=registered.spec.id, args={}))
    run_id = str(started.run_id)
    _advance_entry_to_gate(project, run_id)

    bad = fsm_resolve_gate(
        ResolveGateInput(
            run_id=run_id,
            state_entry_seq=0,
        )
    )
    assert hasattr(bad, "error"), f"expected error envelope, got: {bad!r}"
    assert bad.error == "gate_value_or_binding_required"


def test_binding_on_llm_supplied_gate_rejects_with_source_kind_mismatch(
    project: Project,
) -> None:
    """A `binding` on an ``llm_supplied`` gate rejects with the typed code.

    Without source_kind enforcement, this path would persist an
    unintended cross-run dependency in the ``gate_bindings`` topology
    index.
    """

    spec = _llm_supplied_spec()
    registered = project.register_spec(spec)
    started = fsm_start_run(StartRunInput(spec_id=registered.spec.id, args={}))
    run_id = str(started.run_id)
    _advance_entry_to_gate(project, run_id)

    binding = GateBinding(
        source_run_id=str(uuid.uuid4()),
        source_state_id="qa",
        source_field="review_verdict",
        target_field="review_verdict",
    )
    bad = fsm_resolve_gate(
        ResolveGateInput(
            run_id=run_id,
            state_entry_seq=0,
            binding=binding.model_dump(),
        )
    )
    assert hasattr(bad, "error"), f"expected error envelope, got: {bad!r}"
    assert bad.error == "gate_source_kind_mismatch"

    # The rejected resolution must not leak a binding row.
    with project.session_factory() as session:
        records = project.gates.by_target_run(session, run_id)
    assert records == []


def test_value_on_run_output_gate_rejects_with_source_kind_mismatch(
    project: Project,
) -> None:
    """A literal `value` on a ``run_output`` gate rejects with the typed code.

    Without source_kind enforcement, this path would bypass the
    binding-lookup + ``max_age_ms`` staleness semantics.
    """

    spec = _run_output_spec()
    registered = project.register_spec(spec)
    started = fsm_start_run(StartRunInput(spec_id=registered.spec.id, args={}))
    run_id = str(started.run_id)
    _advance_entry_to_gate(project, run_id)

    bad = fsm_resolve_gate(
        ResolveGateInput(
            run_id=run_id,
            state_entry_seq=0,
            value={"review_verdict": "GO"},
        )
    )
    assert hasattr(bad, "error"), f"expected error envelope, got: {bad!r}"
    assert bad.error == "gate_source_kind_mismatch"


# ---------------------------------------------------------------------------
# state_entry_seq contract
# ---------------------------------------------------------------------------


def test_mismatched_state_entry_seq_rejects(project: Project) -> None:
    """An explicit state_entry_seq that does not match the open gate rejects."""

    spec = _llm_supplied_spec()
    registered = project.register_spec(spec)
    started = fsm_start_run(StartRunInput(spec_id=registered.spec.id, args={}))
    assert not hasattr(started, "error"), f"start_run: {started!r}"
    run_id = str(started.run_id)
    _advance_entry_to_gate(project, run_id)

    open_seq = _open_gate_entry_seq(project, run_id, "await_review")
    bad_seq = open_seq + 999  # deliberately wrong but positive

    bad = fsm_resolve_gate(
        ResolveGateInput(
            run_id=run_id,
            state_entry_seq=bad_seq,
            value={"review_verdict": "GO"},
        )
    )
    assert hasattr(bad, "error"), f"expected error envelope, got: {bad!r}"
    assert bad.error == "gate_state_entry_not_found"

    # No corruption: the binding index must be empty after the rejected
    # resolution (no target_state_entry_seq=0 row leaked through).
    with project.session_factory() as session:
        records = project.gates.by_target_run(session, run_id)
    assert records == []


# ---------------------------------------------------------------------------
# Engine-level gate_pending kind
# ---------------------------------------------------------------------------


def test_engine_advance_returns_gate_pending_when_next_state_is_gate() -> None:
    """advance() into a gate state returns kind=gate_pending, not advance."""

    from ctxr.fsm.core.engine import advance as engine_advance
    from ctxr.fsm.core.models import EngineAdvanceKind, RunCtx

    spec = _llm_supplied_spec()
    ctx = RunCtx(
        run_id=uuid.UUID("00000000-0000-7000-8000-000000000000"),
        fsm_id=spec.id,
        current_state="entry",
        env={},
    )
    result = engine_advance(spec, ctx, {})
    assert result.kind is EngineAdvanceKind.gate_pending
    assert result.next_state == "await_review"
    assert result.brief is not None
    assert result.brief.gate is not None


# ---------------------------------------------------------------------------
# Type stubs used above
# ---------------------------------------------------------------------------


# ``Any`` re-export keeps the type-only import dam happy in case the
# test module is consumed by a tooling pass that prunes unused imports.
_ = Any
