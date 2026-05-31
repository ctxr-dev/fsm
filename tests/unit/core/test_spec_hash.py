"""Unit tests for ``ctxr.fsm.core.spec``.

Covers:

* :func:`fsm_spec_hash` -- canonical JSON hashing semantics:
  - logically equivalent specs (whitespace-only / field-order differences
    in the construction inputs) hash identically;
  - a meaningful change to a field flips the digest.
* :func:`validate_fsm_spec` -- structural checks beyond Pydantic schema:
  - unreachable states surface in ``unreachable_states``;
  - dangling transition targets surface in ``dangling_transitions``;
  - invalid predicate expressions surface in ``invalid_predicates`` when
    the resolved predicate validator raises (the integration contract
    documented in ``spec.py``).
"""

from __future__ import annotations

from typing import Any

from ctxr.fsm.core import spec as spec_module
from ctxr.fsm.core.models import (
    FsmSpec,
    Predicate,
    State,
    Transition,
)
from ctxr.fsm.core.spec import (
    FsmValidationResult,
    fsm_spec_hash,
    validate_fsm_spec,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_simple_spec(**overrides: Any) -> FsmSpec:
    """Construct a minimal two-state spec with one ``always`` transition.

    Keyword overrides are applied on top of the defaults so individual
    tests can vary the entry state, version, or state list while keeping
    the boilerplate to a minimum.
    """
    defaults: dict[str, Any] = {
        "id": "simple",
        "version": 1,
        "entry": "a",
        "states": [
            State(
                id="a",
                purpose="start",
                transitions=[Transition(to="b", when="always")],
            ),
            State(id="b", purpose="end"),
        ],
    }
    defaults.update(overrides)
    return FsmSpec(**defaults)


# ---------------------------------------------------------------------------
# fsm_spec_hash -- canonicalisation
# ---------------------------------------------------------------------------


def test_hash_is_stable_sha256_hex_string() -> None:
    """The digest is a lowercase 64-char hex string."""
    spec = _build_simple_spec()
    digest = fsm_spec_hash(spec)
    assert isinstance(digest, str)
    assert len(digest) == 64
    assert all(ch in "0123456789abcdef" for ch in digest)


def test_hash_is_deterministic_for_identical_inputs() -> None:
    """Two specs built from identical inputs hash to the same digest."""
    spec_one = _build_simple_spec()
    spec_two = _build_simple_spec()
    assert fsm_spec_hash(spec_one) == fsm_spec_hash(spec_two)


def test_hash_ignores_construction_kwarg_order() -> None:
    """Reordering the kwargs to ``FsmSpec(...)`` must not change the hash.

    The canonical JSON dump sorts keys, so a different keyword-argument
    presentation order at construction time still yields a byte-identical
    payload before hashing.
    """
    states = [
        State(
            id="a",
            purpose="start",
            transitions=[Transition(to="b", when="always")],
        ),
        State(id="b", purpose="end"),
    ]
    natural = FsmSpec(id="simple", version=1, entry="a", states=states)
    shuffled = FsmSpec(states=states, entry="a", version=1, id="simple")
    assert fsm_spec_hash(natural) == fsm_spec_hash(shuffled)


def test_hash_ignores_state_field_kwarg_order() -> None:
    """Reordering kwargs of a nested ``State`` must not change the hash."""
    natural_states = [
        State(
            id="a",
            purpose="start",
            preconditions=["x", "y"],
            outputs=["o"],
            transitions=[Transition(to="b", when="always")],
        ),
        State(id="b", purpose="end"),
    ]
    shuffled_states = [
        State(
            transitions=[Transition(when="always", to="b")],
            outputs=["o"],
            preconditions=["x", "y"],
            purpose="start",
            id="a",
        ),
        State(purpose="end", id="b"),
    ]
    natural = FsmSpec(id="simple", entry="a", states=natural_states)
    shuffled = FsmSpec(id="simple", entry="a", states=shuffled_states)
    assert fsm_spec_hash(natural) == fsm_spec_hash(shuffled)


def test_hash_changes_when_meaningful_field_changes() -> None:
    """A semantic edit -- here, the spec id -- must flip the digest."""
    base = _build_simple_spec()
    edited = _build_simple_spec(id="renamed")
    assert fsm_spec_hash(base) != fsm_spec_hash(edited)


def test_hash_changes_when_version_changes() -> None:
    """Bumping the version is a meaningful change and must flip the digest."""
    base = _build_simple_spec(version=1)
    bumped = _build_simple_spec(version=2)
    assert fsm_spec_hash(base) != fsm_spec_hash(bumped)


def test_hash_changes_when_transition_target_changes() -> None:
    """Retargeting a transition produces a different canonical payload."""
    base = _build_simple_spec()
    rerouted_states = [
        State(
            id="a",
            purpose="start",
            transitions=[Transition(to="c", when="always")],
        ),
        State(id="b", purpose="end"),
        State(id="c", purpose="end_alt"),
    ]
    rerouted = FsmSpec(id="simple", entry="a", states=rerouted_states)
    assert fsm_spec_hash(base) != fsm_spec_hash(rerouted)


def test_hash_changes_when_state_purpose_text_changes() -> None:
    """A change to a nested string field must propagate to the digest."""
    base = _build_simple_spec()
    altered_states = [
        State(
            id="a",
            purpose="start NEW",
            transitions=[Transition(to="b", when="always")],
        ),
        State(id="b", purpose="end"),
    ]
    altered = FsmSpec(id="simple", entry="a", states=altered_states)
    assert fsm_spec_hash(base) != fsm_spec_hash(altered)


# ---------------------------------------------------------------------------
# validate_fsm_spec -- happy path
# ---------------------------------------------------------------------------


def test_validate_clean_spec_returns_valid_true() -> None:
    """A well-formed spec produces a result with no errors collected."""
    spec = _build_simple_spec()
    result = validate_fsm_spec(spec)
    assert isinstance(result, FsmValidationResult)
    assert result.valid is True
    assert result.errors == []
    assert result.unreachable_states == []
    assert result.dangling_transitions == []
    assert result.invalid_predicates == []


# ---------------------------------------------------------------------------
# validate_fsm_spec -- unreachable states
# ---------------------------------------------------------------------------


def test_validate_flags_unreachable_states() -> None:
    """A state with no inbound transition from entry is reported."""
    states = [
        State(
            id="a",
            transitions=[Transition(to="b", when="always")],
        ),
        State(id="b"),
        State(id="orphan"),
    ]
    spec = FsmSpec(id="reach", entry="a", states=states)
    result = validate_fsm_spec(spec)
    assert result.valid is False
    assert "orphan" in result.unreachable_states
    # The unreachable diagnostic should NOT have been collected as a
    # dangling-transition or invalid-predicate problem.
    assert result.dangling_transitions == []
    assert result.invalid_predicates == []
    assert any("orphan" in msg for msg in result.errors)


def test_validate_reports_all_unreachable_states_sorted() -> None:
    """Multiple orphans surface together (sorted) in the result."""
    states = [
        State(id="a", transitions=[Transition(to="b", when="always")]),
        State(id="b"),
        State(id="orphan_z"),
        State(id="orphan_a"),
    ]
    spec = FsmSpec(id="reach", entry="a", states=states)
    result = validate_fsm_spec(spec)
    assert result.valid is False
    assert result.unreachable_states == sorted(result.unreachable_states)
    assert set(result.unreachable_states) == {"orphan_a", "orphan_z"}


# ---------------------------------------------------------------------------
# validate_fsm_spec -- dangling transitions
# ---------------------------------------------------------------------------


def test_validate_flags_dangling_transitions() -> None:
    """A transition target that does not exist is reported as dangling."""
    states = [
        State(
            id="a",
            transitions=[Transition(to="ghost", when="always")],
        ),
    ]
    spec = FsmSpec(id="dang", entry="a", states=states)
    result = validate_fsm_spec(spec)
    assert result.valid is False
    assert ("a", "ghost") in result.dangling_transitions
    assert any("ghost" in msg for msg in result.errors)


def test_validate_reports_multiple_dangling_transitions() -> None:
    """Every dangling target is recorded as an ``(from, to)`` pair."""
    states = [
        State(
            id="a",
            transitions=[
                Transition(to="b", when="always"),
                Transition(to="ghost1", when="otherwise"),
            ],
        ),
        State(
            id="b",
            transitions=[Transition(to="ghost2", when="always")],
        ),
    ]
    spec = FsmSpec(id="dang2", entry="a", states=states)
    result = validate_fsm_spec(spec)
    assert result.valid is False
    assert ("a", "ghost1") in result.dangling_transitions
    assert ("b", "ghost2") in result.dangling_transitions
    assert len(result.dangling_transitions) == 2


# ---------------------------------------------------------------------------
# validate_fsm_spec -- invalid predicates
# ---------------------------------------------------------------------------


def test_validate_flags_invalid_predicates_via_validator_raise(
    monkeypatch: Any,
) -> None:
    """``validate_fsm_spec`` reports predicate failures from the validator.

    The integration contract documented in ``spec.py`` is that the
    resolved validator is expected to RAISE on a malformed expression
    and return ``None`` (or any value) on success.  We exercise that
    contract directly by monkey-patching the resolver to return a
    validator that raises for one known-bad expression.
    """
    bad_expression = "this is structurally invalid ("

    def _fake_validator(expression: str) -> None:
        if expression == bad_expression:
            raise ValueError("synthetic parse failure")

    monkeypatch.setattr(
        spec_module, "_resolve_predicate_validator", lambda: _fake_validator
    )

    states = [
        State(
            id="a",
            transitions=[
                Transition(to="b", when=Predicate(bad_expression)),
            ],
        ),
        State(id="b"),
    ]
    spec = FsmSpec(id="pred", entry="a", states=states)
    result = validate_fsm_spec(spec)
    assert result.valid is False
    assert len(result.invalid_predicates) == 1
    location, expression = result.invalid_predicates[0]
    assert expression == bad_expression
    assert location == "state:a/transition[0].when"
    assert any("failed to parse" in msg for msg in result.errors)


def test_validate_flags_invalid_post_validation_predicates(
    monkeypatch: Any,
) -> None:
    """Bad ``post_validations`` expressions are located precisely."""
    bad_expression = "still invalid >>>"

    def _fake_validator(expression: str) -> None:
        if expression == bad_expression:
            raise ValueError("synthetic parse failure")

    monkeypatch.setattr(
        spec_module, "_resolve_predicate_validator", lambda: _fake_validator
    )

    states = [
        State(
            id="a",
            post_validations=[
                Predicate("foo == 1"),
                Predicate(bad_expression),
            ],
            transitions=[Transition(to="b", when="always")],
        ),
        State(id="b"),
    ]
    spec = FsmSpec(id="pred", entry="a", states=states)
    result = validate_fsm_spec(spec)
    assert result.valid is False
    assert len(result.invalid_predicates) == 1
    location, expression = result.invalid_predicates[0]
    assert expression == bad_expression
    assert location == "state:a/post_validations[1]"


def test_validate_skips_predicate_checks_when_validator_unavailable(
    monkeypatch: Any,
) -> None:
    """Missing validator should not fail validation -- check is best-effort."""
    monkeypatch.setattr(
        spec_module, "_resolve_predicate_validator", lambda: None
    )
    # Build a spec with a syntactically-bogus predicate; without a
    # validator, the check is skipped silently and the rest of the spec
    # still passes structural validation.
    states = [
        State(
            id="a",
            transitions=[
                Transition(to="b", when=Predicate("clearly !@#$ broken")),
            ],
        ),
        State(id="b"),
    ]
    spec = FsmSpec(id="pred", entry="a", states=states)
    result = validate_fsm_spec(spec)
    assert result.invalid_predicates == []
    # No other defects either, so the whole spec passes.
    assert result.valid is True
