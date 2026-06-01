"""W23g: tests for the Gate / GateBinding Pydantic models + StateKind.gate.

Covers the substrate layer added by W23g: the closed-vocabulary enum
member, the body model + binding sub-model, the State.kind dispatch,
and the consistency validator that keeps `gate` exclusive with the
other state body kinds.
"""

from __future__ import annotations

import pytest

from ctxr.fsm.core.models import (
    Gate,
    GateBinding,
    GateSourceKind,
    InlineSpec,
    Loop,
    ResponseSchema,
    State,
    StateKind,
    Transition,
    TransitionKind,
    Worker,
)

_SCHEMA = ResponseSchema(
    schema_={
        "type": "object",
        "required": ["verdict"],
        "properties": {"verdict": {"type": "string"}},
    }
)


# ---------------------------------------------------------------------------
# GateSourceKind enum
# ---------------------------------------------------------------------------


def test_gate_source_kind_has_expected_members() -> None:
    assert {m.value for m in GateSourceKind} == {"run_output", "llm_supplied"}


# ---------------------------------------------------------------------------
# GateBinding
# ---------------------------------------------------------------------------


def test_gate_binding_accepts_minimal_payload() -> None:
    b = GateBinding(
        source_state_id="qa",
        source_field="verdict",
        target_field="review_verdict",
    )
    assert b.source_run_id is None
    assert b.source_spec_slug is None
    assert b.source_state_id == "qa"


def test_gate_binding_rejects_empty_string_fields() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        GateBinding(source_state_id="", source_field="x", target_field="y")
    with pytest.raises(ValueError, match="non-empty"):
        GateBinding(source_state_id="x", source_field="   ", target_field="y")


def test_gate_binding_is_frozen() -> None:
    b = GateBinding(
        source_state_id="qa", source_field="verdict", target_field="v"
    )
    with pytest.raises(Exception):  # noqa: B017 - Pydantic frozen-instance
        b.source_run_id = "run-uuidv7"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


def test_gate_run_output_with_bindings_is_valid() -> None:
    g = Gate(
        source_kind=GateSourceKind.run_output,
        response_schema=_SCHEMA,
        bindings=[
            GateBinding(
                source_state_id="qa",
                source_field="verdict",
                target_field="review_verdict",
            )
        ],
    )
    assert g.source_kind is GateSourceKind.run_output
    assert len(g.bindings) == 1


def test_gate_llm_supplied_with_bindings_rejects() -> None:
    with pytest.raises(ValueError, match="bindings must be empty"):
        Gate(
            source_kind=GateSourceKind.llm_supplied,
            response_schema=_SCHEMA,
            bindings=[
                GateBinding(
                    source_state_id="qa",
                    source_field="verdict",
                    target_field="v",
                )
            ],
        )


def test_gate_llm_supplied_without_bindings_is_valid() -> None:
    g = Gate(source_kind=GateSourceKind.llm_supplied, response_schema=_SCHEMA)
    assert g.bindings == []


def test_gate_max_age_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        Gate(
            source_kind=GateSourceKind.run_output,
            response_schema=_SCHEMA,
            max_age_ms=0,
        )
    with pytest.raises(ValueError, match="positive integer"):
        Gate(
            source_kind=GateSourceKind.run_output,
            response_schema=_SCHEMA,
            max_age_ms=-100,
        )


def test_gate_max_age_none_is_valid() -> None:
    g = Gate(
        source_kind=GateSourceKind.run_output,
        response_schema=_SCHEMA,
        max_age_ms=None,
    )
    assert g.max_age_ms is None


# ---------------------------------------------------------------------------
# StateKind.gate + State.kind dispatch
# ---------------------------------------------------------------------------


def test_statekind_gate_is_distinct_member() -> None:
    assert StateKind.gate.value == "gate"
    # Distinct from existing kinds.
    assert StateKind.gate is not StateKind.worker
    assert StateKind.gate is not StateKind.terminal


def test_state_with_gate_body_has_kind_gate() -> None:
    state = State(
        id="await_review",
        gate=Gate(
            source_kind=GateSourceKind.llm_supplied,
            response_schema=_SCHEMA,
        ),
        outputs=["review_verdict"],
        transitions=[Transition(to="ship", when=TransitionKind.always)],
    )
    assert state.kind is StateKind.gate


def test_state_consistency_blocks_gate_with_worker() -> None:
    with pytest.raises(ValueError, match="cannot be combined"):
        State(
            id="bad",
            gate=Gate(
                source_kind=GateSourceKind.llm_supplied,
                response_schema=_SCHEMA,
            ),
            worker=Worker(
                role="x",
                prompt_template="x",
                inputs=[],
                response_schema=_SCHEMA,
            ),
            outputs=["verdict"],
            transitions=[Transition(to="end", when=TransitionKind.always)],
        )


def test_state_consistency_blocks_gate_with_loop() -> None:
    loop = Loop(
        worker=Worker(
            role="x",
            prompt_template="x",
            inputs=[],
            response_schema=ResponseSchema(
                schema_={
                    "type": "object",
                    "properties": {"done": {"type": "boolean"}},
                    "required": ["done"],
                }
            ),
        ),
        max_iterations=5,
        done_field="done",
    )
    with pytest.raises(ValueError, match="cannot be combined"):
        State(
            id="bad",
            gate=Gate(
                source_kind=GateSourceKind.llm_supplied,
                response_schema=_SCHEMA,
            ),
            loop=loop,
            outputs=["verdict"],
            transitions=[Transition(to="end", when=TransitionKind.always)],
        )


def test_state_consistency_blocks_gate_with_inline() -> None:
    with pytest.raises(ValueError, match="cannot be combined"):
        State(
            id="bad",
            gate=Gate(
                source_kind=GateSourceKind.llm_supplied,
                response_schema=_SCHEMA,
            ),
            inline=InlineSpec(handler_id="x", response_schema=_SCHEMA),
            outputs=["verdict"],
            transitions=[Transition(to="end", when=TransitionKind.always)],
        )


def test_state_gate_without_outputs_or_transitions_still_typed_as_gate() -> None:
    # The kind property dispatches on body fields first, only falling
    # through to "terminal" when no body is set.
    state = State(
        id="awaiting",
        gate=Gate(
            source_kind=GateSourceKind.llm_supplied,
            response_schema=_SCHEMA,
        ),
    )
    assert state.kind is StateKind.gate


# ---------------------------------------------------------------------------
# Brief.gate surface + EventKind extensions
# ---------------------------------------------------------------------------


def test_event_kind_has_gate_members() -> None:
    from ctxr.fsm.core.models import EventKind

    assert EventKind.gate_resolved.value == "gate_resolved"
    assert EventKind.gate_resolution_failed.value == "gate_resolution_failed"
    assert EventKind.gate_binding_recorded.value == "gate_binding_recorded"


def test_build_brief_surfaces_gate_when_state_is_gate() -> None:
    import uuid as _uuid

    from ctxr.fsm.core.engine import build_brief

    state = State(
        id="await_review",
        gate=Gate(
            source_kind=GateSourceKind.llm_supplied,
            response_schema=_SCHEMA,
        ),
        outputs=["review_verdict"],
        transitions=[Transition(to="ship", when=TransitionKind.always)],
    )
    # FsmSpec stub is only used for its id; build the smallest legal spec.
    from ctxr.fsm.core.models import FsmSpec

    spec = FsmSpec(
        id="demo",
        version=1,
        entry="await_review",
        states=[
            state,
            State(id="ship", transitions=[]),
        ],
    )
    brief = build_brief(spec, state, env={}, run_id=_uuid.uuid4())

    assert brief.gate is not None
    assert brief.gate.source_kind is GateSourceKind.llm_supplied
    # Has no worker / loop — consumers branch on gate to switch from
    # commit_outputs to resolve_gate.
    assert brief.has_worker is False
    assert brief.has_loop is False
    assert brief.worker is None
    assert brief.loop is None


def test_build_brief_carries_no_gate_for_non_gate_states() -> None:
    import uuid as _uuid

    from ctxr.fsm.core.engine import build_brief
    from ctxr.fsm.core.models import FsmSpec

    state = State(
        id="plan",
        worker=Worker(
            role="planner",
            prompt_template="do the work",
            inputs=[],
            response_schema=_SCHEMA,
        ),
        outputs=["verdict"],
        transitions=[Transition(to="end", when=TransitionKind.always)],
    )
    spec = FsmSpec(
        id="demo",
        version=1,
        entry="plan",
        states=[state, State(id="end", transitions=[])],
    )
    brief = build_brief(spec, state, env={}, run_id=_uuid.uuid4())

    assert brief.gate is None
    assert brief.has_worker is True
