"""Tests for the post-validation step of the FSM engine.

Covers both :func:`ctxr.fsm.core.engine.run_post_validations` (the pure
predicate-batch evaluator) and the way :func:`ctxr.fsm.core.engine.advance`
surfaces post-validation failure as ``reason="post_validation_failed"``.
"""

from __future__ import annotations

from uuid import uuid4

from ctxr.fsm.core.engine import advance, run_post_validations
from ctxr.fsm.core.models import (
    FsmSpec,
    Predicate,
    RunCtx,
    State,
    Transition,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_state(
    *,
    state_id: str = "s_check",
    post_validations: list[Predicate] | None = None,
    transitions: list[Transition] | None = None,
) -> State:
    """Build a workerless state with the given post-validations + transitions.

    A workerless state has no response schema so :func:`validate_output`
    is trivially satisfied — that keeps each test focused on
    post-validation behaviour.
    """
    return State(
        id=state_id,
        purpose="post-validation test",
        worker=None,
        loop=None,
        post_validations=list(post_validations or []),
        transitions=list(transitions or []),
    )


def _make_spec(state: State, terminal_id: str = "s_done") -> FsmSpec:
    """Wrap ``state`` in a minimal :class:`FsmSpec` with a terminal sibling."""
    terminal = State(id=terminal_id, purpose="terminal")
    return FsmSpec(
        id="test_fsm",
        version=1,
        entry=state.id,
        states=[state, terminal],
    )


def _make_run_ctx(spec: FsmSpec, state: State) -> RunCtx:
    """Build a fresh :class:`RunCtx` positioned at ``state``."""
    return RunCtx(
        run_id=uuid4(),
        fsm_id=spec.id,
        current_state=state.id,
        env={},
    )


# ---------------------------------------------------------------------------
# run_post_validations — direct unit tests
# ---------------------------------------------------------------------------


def test_empty_post_validations_is_trivially_valid() -> None:
    """A state with no post-validations always passes (and has no entries)."""
    state = _make_state(post_validations=[])
    result = run_post_validations(state, outputs={"any": "thing"})

    assert result.valid is True
    assert result.results == []


def test_single_true_predicate_passes() -> None:
    """A single true predicate yields ``valid=True`` and a true entry."""
    state = _make_state(
        post_validations=[Predicate("count == 3")],
    )
    result = run_post_validations(state, outputs={"count": 3})

    assert result.valid is True
    assert len(result.results) == 1
    entry = result.results[0]
    assert entry.result is True
    assert entry.expression == "count == 3"
    assert entry.error is None
    assert entry.check == "post_validation"


def test_single_false_predicate_fails() -> None:
    """A single false predicate yields ``valid=False`` with a false entry."""
    state = _make_state(
        post_validations=[Predicate("count == 3")],
    )
    result = run_post_validations(state, outputs={"count": 4})

    assert result.valid is False
    assert len(result.results) == 1
    entry = result.results[0]
    assert entry.result is False
    assert entry.expression == "count == 3"
    assert entry.error is None


def test_multiple_predicates_and_composed_all_true() -> None:
    """Multiple predicates that are all true keep ``valid=True``."""
    state = _make_state(
        post_validations=[
            Predicate("count == 3"),
            Predicate("status == 'ok'"),
            Predicate("len(items) >= 1"),
        ],
    )
    result = run_post_validations(
        state,
        outputs={"count": 3, "status": "ok", "items": ["a", "b"]},
    )

    assert result.valid is True
    assert len(result.results) == 3
    assert all(entry.result is True for entry in result.results)
    assert all(entry.error is None for entry in result.results)


def test_multiple_predicates_and_composed_one_false_fails() -> None:
    """A single false predicate among several fails the AND-composition."""
    state = _make_state(
        post_validations=[
            Predicate("count == 3"),
            Predicate("status == 'ok'"),  # this one will be false
            Predicate("len(items) >= 1"),
        ],
    )
    result = run_post_validations(
        state,
        outputs={"count": 3, "status": "bad", "items": ["a", "b"]},
    )

    assert result.valid is False
    assert len(result.results) == 3
    # Every predicate is still evaluated and reported, in declared order.
    results_by_expr = {entry.expression: entry for entry in result.results}
    assert results_by_expr["count == 3"].result is True
    assert results_by_expr["status == 'ok'"].result is False
    assert results_by_expr["len(items) >= 1"].result is True


def test_malformed_predicate_surfaces_error_and_fails() -> None:
    """A malformed expression is captured on ``error`` with ``result=False``."""
    # ``Predicate`` itself only requires a non-empty string, so a
    # syntactically broken expression is accepted at construction time
    # and surfaces at evaluation. This mirrors how the engine sees a
    # bad guard authored upstream.
    state = _make_state(
        post_validations=[Predicate("count == ==")],
    )
    result = run_post_validations(state, outputs={"count": 3})

    assert result.valid is False
    assert len(result.results) == 1
    entry = result.results[0]
    assert entry.result is False
    assert entry.error is not None
    assert entry.error != ""
    assert entry.expression == "count == =="


# ---------------------------------------------------------------------------
# advance() — post-validation failure must surface as a structured fault
# ---------------------------------------------------------------------------


def test_advance_faults_with_post_validation_failed_reason() -> None:
    """When a post-validation fails, ``advance`` returns a structured fault.

    The ``reason`` must be exactly ``"post_validation_failed"`` and the
    ``post_validations`` field must carry the per-predicate breakdown so
    the journal layer can record the diagnostic.
    """
    state = _make_state(
        post_validations=[Predicate("count == 3")],
        transitions=[Transition(to="s_done", when="always")],
    )
    spec = _make_spec(state)
    run_ctx = _make_run_ctx(spec, state)

    result = advance(spec, run_ctx, outputs={"count": 4})

    assert result.kind == "fault"
    assert result.reason == "post_validation_failed"
    assert len(result.post_validations) == 1
    assert result.post_validations[0].result is False
    assert result.post_validations[0].expression == "count == 3"
    # No transition should have been resolved on a post-validation fault.
    assert result.evaluations == []
    assert result.next_state is None
    assert result.brief is None


def test_advance_passes_post_validations_then_advances() -> None:
    """When every post-validation passes, ``advance`` proceeds to transitions."""
    state = _make_state(
        post_validations=[
            Predicate("count == 3"),
            Predicate("status == 'ok'"),
        ],
        transitions=[Transition(to="s_done", when="always")],
    )
    spec = _make_spec(state)
    run_ctx = _make_run_ctx(spec, state)

    result = advance(spec, run_ctx, outputs={"count": 3, "status": "ok"})

    assert result.kind == "advance"
    assert result.reason is None
    assert result.next_state == "s_done"
    assert result.brief is not None
    assert result.brief.state == "s_done"


def test_advance_surfaces_malformed_post_validation_as_fault() -> None:
    """A malformed post-validation expression faults via the same path."""
    state = _make_state(
        post_validations=[Predicate("count == ==")],
        transitions=[Transition(to="s_done", when="always")],
    )
    spec = _make_spec(state)
    run_ctx = _make_run_ctx(spec, state)

    result = advance(spec, run_ctx, outputs={"count": 3})

    assert result.kind == "fault"
    assert result.reason == "post_validation_failed"
    assert len(result.post_validations) == 1
    entry = result.post_validations[0]
    assert entry.result is False
    assert entry.error is not None
    assert entry.error != ""
