"""Unit tests for ``ctxr.fsm.core.models``.

Covers the spec primitives (FsmSpec, State, Transition, Predicate,
ResponseSchema, VerifierSpec) and a slice of the engine-side value
objects (CommitSignature, CommitToken).

Tests use plain pytest. UUIDs are generated with ``uuid.uuid4`` to
keep the suite self-contained.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from ctxr.fsm.core.models import (
    CommitSignature,
    CommitToken,
    FsmSpec,
    Loop,
    Predicate,
    ResponseSchema,
    State,
    Transition,
    TransitionKind,
    VerifierSpec,
    Worker,
)

# ---------------------------------------------------------------------------
# Helpers (kept inline; promote to _helpers.py only when reused widely)
# ---------------------------------------------------------------------------


def _bool_schema(done_field: str = "done") -> ResponseSchema:
    """Build a minimal ResponseSchema whose properties include ``done_field``."""
    return ResponseSchema(
        schema={
            "type": "object",
            "properties": {done_field: {"type": "boolean"}},
            "required": [done_field],
        }
    )


def _simple_worker(role: str = "worker") -> Worker:
    """Build a minimal Worker with a non-empty role + prompt."""
    return Worker(role=role, prompt_template="do the thing")


def _state(state_id: str, transitions: list[Transition] | None = None) -> State:
    """Build a minimal State with the given id and optional transitions."""
    return State(
        id=state_id,
        purpose=f"state {state_id}",
        worker=_simple_worker(role=state_id),
        transitions=transitions or [],
    )


# ---------------------------------------------------------------------------
# FsmSpec construction
# ---------------------------------------------------------------------------


def test_fsm_spec_constructs_minimal_single_state() -> None:
    spec = FsmSpec(id="demo", entry="start", states=[_state("start")])
    assert spec.id == "demo"
    assert spec.version == 1
    assert spec.entry == "start"
    assert [s.id for s in spec.states] == ["start"]
    assert spec.get_state("start").id == "start"


def test_fsm_spec_multistate_with_transition() -> None:
    states = [
        _state("start", transitions=[Transition(to="finish", when="always")]),
        _state("finish"),
    ]
    spec = FsmSpec(id="demo", entry="start", states=states)
    assert spec.get_state("finish").id == "finish"


def test_fsm_spec_get_state_raises_keyerror_for_unknown_id() -> None:
    spec = FsmSpec(id="demo", entry="start", states=[_state("start")])
    with pytest.raises(KeyError):
        spec.get_state("nope")


# ---------------------------------------------------------------------------
# snake_case state id validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_id",
    [
        "BadId",
        "1starts_with_digit",
        "has-dash",
        "has.dot",
        "",
        "has space",
        "UPPER",
    ],
)
def test_state_id_rejects_non_snake_case(bad_id: str) -> None:
    with pytest.raises(ValidationError):
        State(id=bad_id, worker=_simple_worker())


@pytest.mark.parametrize("good_id", ["a", "a1", "snake_case", "with_123_digits"])
def test_state_id_accepts_snake_case(good_id: str) -> None:
    state = State(id=good_id, worker=_simple_worker(role=good_id))
    assert state.id == good_id


def test_transition_to_rejects_non_snake_case() -> None:
    with pytest.raises(ValidationError):
        Transition(to="BadTarget", when="always")


# ---------------------------------------------------------------------------
# State: loop + worker mutual exclusion
# ---------------------------------------------------------------------------


def test_state_rejects_both_worker_and_loop() -> None:
    loop_worker = Worker(
        role="looper",
        prompt_template="iterate",
        response_schema=_bool_schema(),
    )
    loop = Loop(worker=loop_worker, done_field="done")
    with pytest.raises(ValidationError) as excinfo:
        State(id="conflict", worker=_simple_worker(), loop=loop)
    assert "worker" in str(excinfo.value) and "loop" in str(excinfo.value)


def test_state_accepts_loop_only() -> None:
    loop_worker = Worker(
        role="looper",
        prompt_template="iterate",
        response_schema=_bool_schema("complete"),
    )
    loop = Loop(worker=loop_worker, done_field="complete")
    state = State(id="loop_state", loop=loop)
    assert state.worker is None
    assert state.loop is loop


def test_state_loop_done_field_must_be_in_schema_properties() -> None:
    loop_worker = Worker(
        role="looper",
        prompt_template="iterate",
        response_schema=_bool_schema("done"),
    )
    with pytest.raises(ValidationError):
        State(
            id="bad_loop",
            loop=Loop(worker=loop_worker, done_field="not_in_schema"),
        )


# ---------------------------------------------------------------------------
# FsmSpec: missing entry, duplicate state ids
# ---------------------------------------------------------------------------


def test_fsm_spec_rejects_unknown_entry() -> None:
    with pytest.raises(ValidationError) as excinfo:
        FsmSpec(id="demo", entry="ghost", states=[_state("start")])
    assert "entry" in str(excinfo.value)


def test_fsm_spec_rejects_duplicate_state_ids() -> None:
    with pytest.raises(ValidationError) as excinfo:
        FsmSpec(
            id="demo",
            entry="dup",
            states=[_state("dup"), _state("dup")],
        )
    assert "unique" in str(excinfo.value)


def test_fsm_spec_rejects_empty_states() -> None:
    with pytest.raises(ValidationError):
        FsmSpec(id="demo", entry="anything", states=[])


def test_fsm_spec_rejects_empty_id() -> None:
    with pytest.raises(ValidationError):
        FsmSpec(id="", entry="start", states=[_state("start")])


def test_fsm_spec_rejects_zero_version() -> None:
    with pytest.raises(ValidationError):
        FsmSpec(id="demo", version=0, entry="start", states=[_state("start")])


# ---------------------------------------------------------------------------
# Transition.when shape normalisation
# ---------------------------------------------------------------------------


def test_transition_when_always_literal() -> None:
    t = Transition(to="next_state", when="always")
    assert t.when == "always"
    # W14i: the boundary normaliser maps the bare string onto the
    # ``TransitionKind`` enum so downstream code can branch on the
    # typed value rather than on a free-form literal.
    assert t.when is TransitionKind.always


def test_transition_when_otherwise_literal() -> None:
    t = Transition(to="next_state", when="otherwise")
    assert t.when == "otherwise"
    assert t.when is TransitionKind.otherwise


def test_transition_when_bare_string_becomes_predicate() -> None:
    t = Transition(to="next_state", when="x == 1")
    assert isinstance(t.when, Predicate)
    assert t.when.expression == "x == 1"


def test_transition_when_dict_kind_always() -> None:
    t = Transition(to="next_state", when={"kind": "always"})
    assert t.when == "always"


def test_transition_when_dict_kind_otherwise() -> None:
    t = Transition(to="next_state", when={"kind": "otherwise"})
    assert t.when == "otherwise"


def test_transition_when_dict_kind_deterministic() -> None:
    t = Transition(
        to="next_state",
        when={"kind": "deterministic", "expression": "y > 0"},
    )
    assert isinstance(t.when, Predicate)
    assert t.when.expression == "y > 0"


def test_transition_when_dict_kind_deterministic_rejects_empty_expression() -> None:
    with pytest.raises(ValidationError):
        Transition(
            to="next_state",
            when={"kind": "deterministic", "expression": "  "},
        )


def test_transition_when_dict_kind_judgement() -> None:
    t = Transition(
        to="next_state",
        when={
            "kind": "judgement",
            "criteria": "is it good?",
            "evidence_required": True,
        },
    )
    assert isinstance(t.when, dict)
    assert t.when["kind"] == "judgement"
    assert t.when["criteria"] == "is it good?"
    assert t.when["evidence_required"] is True


def test_transition_when_dict_kind_judgement_defaults_evidence_required_false() -> None:
    t = Transition(
        to="next_state",
        when={"kind": "judgement", "criteria": "looks reasonable"},
    )
    assert isinstance(t.when, dict)
    assert t.when["evidence_required"] is False


def test_transition_when_dict_kind_judgement_rejects_empty_criteria() -> None:
    with pytest.raises(ValidationError):
        Transition(to="next_state", when={"kind": "judgement", "criteria": ""})


def test_transition_when_dict_with_expression_only_becomes_predicate() -> None:
    t = Transition(to="next_state", when={"expression": "z != 0"})
    assert isinstance(t.when, Predicate)
    assert t.when.expression == "z != 0"


def test_transition_when_dict_with_unknown_shape_rejected() -> None:
    with pytest.raises(ValidationError):
        Transition(to="next_state", when={"foo": "bar"})


def test_transition_when_predicate_instance_passes_through() -> None:
    pred = Predicate("a == b")
    t = Transition(to="next_state", when=pred)
    assert isinstance(t.when, Predicate)
    assert t.when.expression == "a == b"


# ---------------------------------------------------------------------------
# ResponseSchema.model_validate_json_payload happy + sad path
# ---------------------------------------------------------------------------


def test_response_schema_validates_conforming_payload() -> None:
    rs = ResponseSchema(
        schema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "count": {"type": "integer"},
            },
            "required": ["name", "count"],
        }
    )
    valid, errors = rs.model_validate_json_payload({"name": "x", "count": 3})
    assert valid is True
    assert errors == []


def test_response_schema_rejects_nonconforming_payload() -> None:
    rs = ResponseSchema(
        schema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "count": {"type": "integer"},
            },
            "required": ["name", "count"],
        }
    )
    valid, errors = rs.model_validate_json_payload({"name": 7})
    assert valid is False
    assert errors  # non-empty
    assert any("count" in msg or "<root>" in msg for msg in errors)


def test_response_schema_alias_round_trip() -> None:
    rs = ResponseSchema(schema={"type": "object"})
    assert rs.schema_ == {"type": "object"}


# ---------------------------------------------------------------------------
# Predicate empty rejection
# ---------------------------------------------------------------------------


def test_predicate_rejects_empty_string() -> None:
    with pytest.raises(ValidationError):
        Predicate("")


def test_predicate_rejects_whitespace_only() -> None:
    with pytest.raises(ValidationError):
        Predicate("   ")


def test_predicate_accepts_simple_expression() -> None:
    p = Predicate("count > 0")
    assert p.expression == "count > 0"


def test_predicate_keyword_construction_equivalent() -> None:
    p = Predicate(expression="count > 0")
    assert p.expression == "count > 0"


# ---------------------------------------------------------------------------
# VerifierSpec parallel_count >= majority_threshold
# ---------------------------------------------------------------------------


def test_verifier_spec_defaults_are_consistent() -> None:
    vs = VerifierSpec(
        role="judge",
        prompt_template="judge this",
        response_schema=_bool_schema(),
    )
    assert vs.parallel_count == 3
    assert vs.majority_threshold == 2


def test_verifier_spec_rejects_parallel_count_below_majority() -> None:
    with pytest.raises(ValidationError) as excinfo:
        VerifierSpec(
            role="judge",
            prompt_template="judge",
            response_schema=_bool_schema(),
            majority_threshold=3,
            parallel_count=2,
        )
    assert "parallel_count" in str(excinfo.value)


def test_verifier_spec_accepts_parallel_count_equal_to_majority() -> None:
    vs = VerifierSpec(
        role="judge",
        prompt_template="judge",
        response_schema=_bool_schema(),
        majority_threshold=3,
        parallel_count=3,
    )
    assert vs.parallel_count == 3
    assert vs.majority_threshold == 3


def test_verifier_spec_rejects_zero_majority_threshold() -> None:
    with pytest.raises(ValidationError):
        VerifierSpec(
            role="judge",
            prompt_template="judge",
            response_schema=_bool_schema(),
            majority_threshold=0,
            parallel_count=3,
        )


# ---------------------------------------------------------------------------
# CommitSignature.compute determinism
# ---------------------------------------------------------------------------


def test_commit_signature_compute_is_deterministic_for_same_inputs() -> None:
    brief_id = uuid4()
    inputs: dict[str, Any] = {"a": 1, "b": [1, 2, 3]}
    outputs: dict[str, Any] = {"ok": True, "items": ["x", "y"]}
    session_id = "session-abc"

    sig1 = CommitSignature.compute(brief_id, inputs, outputs, session_id)
    sig2 = CommitSignature.compute(brief_id, inputs, outputs, session_id)

    assert sig1.signature == sig2.signature
    assert sig1.inputs_hash == sig2.inputs_hash
    assert sig1.outputs_hash == sig2.outputs_hash
    assert sig1.brief_id == sig2.brief_id == brief_id
    assert sig1.session_id == sig2.session_id == session_id


def test_commit_signature_compute_independent_of_dict_key_order() -> None:
    brief_id = uuid4()
    session_id = "s"
    sig1 = CommitSignature.compute(brief_id, {"a": 1, "b": 2}, {"x": 1}, session_id)
    sig2 = CommitSignature.compute(brief_id, {"b": 2, "a": 1}, {"x": 1}, session_id)
    assert sig1.signature == sig2.signature


def test_commit_signature_compute_changes_with_session_id() -> None:
    brief_id = uuid4()
    inputs: dict[str, Any] = {"a": 1}
    outputs: dict[str, Any] = {"ok": True}

    sig_a = CommitSignature.compute(brief_id, inputs, outputs, "session-A")
    sig_b = CommitSignature.compute(brief_id, inputs, outputs, "session-B")

    assert sig_a.signature != sig_b.signature
    # The component hashes that don't include session_id remain equal.
    assert sig_a.inputs_hash == sig_b.inputs_hash
    assert sig_a.outputs_hash == sig_b.outputs_hash


def test_commit_signature_compute_changes_with_inputs() -> None:
    brief_id = uuid4()
    sig_a = CommitSignature.compute(brief_id, {"a": 1}, {"ok": True}, "s")
    sig_b = CommitSignature.compute(brief_id, {"a": 2}, {"ok": True}, "s")
    assert sig_a.signature != sig_b.signature
    assert sig_a.inputs_hash != sig_b.inputs_hash


def test_commit_signature_compute_changes_with_outputs() -> None:
    brief_id = uuid4()
    sig_a = CommitSignature.compute(brief_id, {"a": 1}, {"ok": True}, "s")
    sig_b = CommitSignature.compute(brief_id, {"a": 1}, {"ok": False}, "s")
    assert sig_a.signature != sig_b.signature
    assert sig_a.outputs_hash != sig_b.outputs_hash


# ---------------------------------------------------------------------------
# CommitToken.issue sets expires_at correctly
# ---------------------------------------------------------------------------


def test_commit_token_issue_sets_expires_at_in_future_utc() -> None:
    run_id = uuid4()
    before = datetime.now(tz=UTC)
    token = CommitToken.issue(run_id, "src_state", "dst_state", ttl_seconds=60)
    after = datetime.now(tz=UTC)

    assert token.run_id == run_id
    assert token.state_id == "src_state"
    assert token.expected_next_state == "dst_state"
    # Token uuid is freshly minted.
    assert token.token != run_id

    # expires_at is in UTC and bracketed by [before+60, after+60].
    assert token.expires_at.tzinfo is not None
    assert token.expires_at.utcoffset() == timedelta(0)
    assert before + timedelta(seconds=60) <= token.expires_at <= after + timedelta(seconds=60)


def test_commit_token_issue_honors_custom_ttl() -> None:
    run_id = uuid4()
    before = datetime.now(tz=UTC)
    token = CommitToken.issue(run_id, "s", "n", ttl_seconds=5)
    after = datetime.now(tz=UTC)

    delta_low = (token.expires_at - before).total_seconds()
    delta_high = (token.expires_at - after).total_seconds()
    # 5-second TTL with a small wallclock allowance.
    assert 4.99 <= delta_low <= 5.01
    assert 4.99 <= delta_high <= 5.01


def test_commit_token_issue_default_ttl_is_sixty_seconds() -> None:
    run_id = uuid4()
    before = datetime.now(tz=UTC)
    token = CommitToken.issue(run_id, "s", "n")
    delta = (token.expires_at - before).total_seconds()
    # Default TTL = 60s; allow a tiny wallclock slack.
    assert 59.99 <= delta <= 60.01


def test_commit_token_issue_rejects_zero_or_negative_ttl() -> None:
    run_id = uuid4()
    with pytest.raises(ValueError):
        CommitToken.issue(run_id, "s", "n", ttl_seconds=0)
    with pytest.raises(ValueError):
        CommitToken.issue(run_id, "s", "n", ttl_seconds=-1)


# ---------------------------------------------------------------------------
# Worker.prompt_template_language (W21 — optional format hint)
# ---------------------------------------------------------------------------


def test_worker_prompt_template_language_defaults_to_none() -> None:
    w = Worker(role="r", prompt_template="x")
    assert w.prompt_template_language is None


@pytest.mark.parametrize("language", ["markdown", "jinja", "plain", "json", "yaml"])
def test_worker_prompt_template_language_accepts_arbitrary_string(language: str) -> None:
    # The field is intentionally free-form (not a closed enum) so
    # consumers own the convention. The library only checks it isn't
    # blank when set.
    w = Worker(role="r", prompt_template="x", prompt_template_language=language)
    assert w.prompt_template_language == language


def test_worker_prompt_template_language_strips_surrounding_whitespace() -> None:
    w = Worker(role="r", prompt_template="x", prompt_template_language="  markdown  ")
    assert w.prompt_template_language == "markdown"


def test_worker_rejects_blank_prompt_template_language() -> None:
    # Empty / whitespace-only is a typo not a deliberate omission;
    # raise rather than silently accept (which would mimic the
    # "no hint" path and hide the bug).
    with pytest.raises(ValidationError):
        Worker(role="r", prompt_template="x", prompt_template_language="")
    with pytest.raises(ValidationError):
        Worker(role="r", prompt_template="x", prompt_template_language="   ")


def test_worker_serialises_prompt_template_language_when_set() -> None:
    w = Worker(role="r", prompt_template="x", prompt_template_language="markdown")
    dumped = w.model_dump()
    assert dumped["prompt_template_language"] == "markdown"


def test_worker_serialises_prompt_template_language_as_null_when_unset() -> None:
    w = Worker(role="r", prompt_template="x")
    dumped = w.model_dump()
    assert dumped["prompt_template_language"] is None


def test_worker_round_trips_through_json_with_language() -> None:
    w = Worker(role="r", prompt_template="x", prompt_template_language="markdown")
    raw = w.model_dump_json()
    restored = Worker.model_validate_json(raw)
    assert restored.prompt_template_language == "markdown"
    assert restored == w


def test_verifier_spec_prompt_template_language_same_contract() -> None:
    schema = ResponseSchema(schema={"type": "object", "properties": {}})
    vs = VerifierSpec(
        role="judge",
        prompt_template="rate the output",
        prompt_template_language="markdown",
        response_schema=schema,
    )
    assert vs.prompt_template_language == "markdown"
    # Same validation as Worker.
    with pytest.raises(ValidationError):
        VerifierSpec(
            role="judge",
            prompt_template="x",
            prompt_template_language="",
            response_schema=schema,
        )
