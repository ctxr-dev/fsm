"""Tests for loop mechanics in :mod:`ctxr.fsm.core.engine` and :mod:`ctxr.fsm.core.loop`.

Covers:

* ``loop_decide``: termination on ``done_field=True``.
* ``loop_decide``: termination on ``max_iterations``.
* ``loop_decide``: ``iteration_n`` echoed back on every decision.
* ``loop_decide``: falsy / non-True done values keep the loop going.
* ``advance()`` on a loop iteration that should continue returns
  ``kind="loop_continue"`` with an incremented ``iteration_n`` and a
  ``brief`` carrying the ``outputs_path`` for the next iteration.
* ``advance()`` on a loop iteration that should terminate proceeds to
  the next state.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from ctxr.fsm.core.engine import advance
from ctxr.fsm.core.loop import decide as loop_decide
from ctxr.fsm.core.models import (
    FsmSpec,
    Loop,
    LoopDecision,
    Predicate,
    ResponseSchema,
    RunCtx,
    State,
    Transition,
    Worker,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _loop_response_schema() -> ResponseSchema:
    """Build a tiny response schema that declares the ``done`` property.

    ``State`` validates that ``loop.done_field`` is declared in the
    worker's response schema properties, so loop tests need a real
    schema that contains it.
    """
    return ResponseSchema.model_validate(
        {
            "schema": {
                "type": "object",
                "properties": {
                    "done": {"type": "boolean"},
                    "note": {"type": "string"},
                },
                "required": ["done"],
            }
        }
    )


def _make_loop_state(
    state_id: str = "looping",
    max_iterations: int = 5,
    iteration_outputs_dir: str | None = None,
    transitions: list[Transition] | None = None,
) -> State:
    """Construct a ``State`` whose body is a single bounded loop."""
    worker = Worker(
        role="iterator",
        prompt_template="iterate",
        inputs=["seed"],
        response_schema=_loop_response_schema(),
    )
    loop = Loop(
        worker=worker,
        max_iterations=max_iterations,
        done_field="done",
        iteration_outputs_dir=iteration_outputs_dir,
    )
    return State(
        id=state_id,
        purpose="loop until done",
        outputs=["done"],
        loop=loop,
        transitions=transitions or [],
    )


def _make_terminal_state(state_id: str = "finished") -> State:
    """A no-worker, no-transition terminal state for advance() tests."""
    return State(
        id=state_id,
        purpose="all done",
    )


def _make_spec_with_loop_and_terminal(
    *,
    max_iterations: int = 5,
    iteration_outputs_dir: str | None = None,
) -> FsmSpec:
    """Build a tiny two-state spec: loop -> terminal on ``always``."""
    terminal = _make_terminal_state("finished")
    looping = _make_loop_state(
        state_id="looping",
        max_iterations=max_iterations,
        iteration_outputs_dir=iteration_outputs_dir,
        transitions=[Transition(to="finished", when="always")],
    )
    return FsmSpec(
        id="loop-spec",
        version=1,
        entry="looping",
        states=[looping, terminal],
    )


# ---------------------------------------------------------------------------
# loop_decide: done_field termination
# ---------------------------------------------------------------------------


def test_loop_decide_terminates_when_done_field_true() -> None:
    state = _make_loop_state(max_iterations=10)

    decision = loop_decide(state, {"done": True}, iteration_n=3)

    assert isinstance(decision, LoopDecision)
    assert decision.is_loop is True
    assert decision.terminate is True
    assert decision.reason == "done_field"
    assert decision.iteration_n == 3


def test_loop_decide_terminates_done_field_takes_precedence_over_max() -> None:
    """``done_field=True`` on the final iteration wins over max_iterations."""
    state = _make_loop_state(max_iterations=3)

    decision = loop_decide(state, {"done": True}, iteration_n=3)

    assert decision.terminate is True
    assert decision.reason == "done_field"
    assert decision.iteration_n == 3


# ---------------------------------------------------------------------------
# loop_decide: max_iterations termination
# ---------------------------------------------------------------------------


def test_loop_decide_terminates_at_max_iterations() -> None:
    state = _make_loop_state(max_iterations=4)

    decision = loop_decide(state, {"done": False}, iteration_n=4)

    assert decision.is_loop is True
    assert decision.terminate is True
    assert decision.reason == "max_iterations"
    assert decision.iteration_n == 4


def test_loop_decide_terminates_when_iteration_exceeds_max() -> None:
    state = _make_loop_state(max_iterations=2)

    decision = loop_decide(state, {"done": False}, iteration_n=5)

    assert decision.terminate is True
    assert decision.reason == "max_iterations"
    assert decision.iteration_n == 5


# ---------------------------------------------------------------------------
# loop_decide: iteration_n is echoed back on continue
# ---------------------------------------------------------------------------


def test_loop_decide_continue_records_iteration_n() -> None:
    state = _make_loop_state(max_iterations=10)

    decision = loop_decide(state, {"done": False}, iteration_n=1)

    assert decision.is_loop is True
    assert decision.terminate is False
    assert decision.reason is None
    assert decision.iteration_n == 1


def test_loop_decide_continue_iteration_n_increments_across_calls() -> None:
    """Successive decisions echo whatever iteration index was passed in."""
    state = _make_loop_state(max_iterations=10)

    for iteration_n in (1, 2, 3, 4):
        decision = loop_decide(state, {"done": False}, iteration_n=iteration_n)
        assert decision.terminate is False
        assert decision.iteration_n == iteration_n


# ---------------------------------------------------------------------------
# loop_decide: falsy / non-True done values keep looping
# ---------------------------------------------------------------------------


def test_loop_decide_continues_when_done_field_missing() -> None:
    state = _make_loop_state(max_iterations=10)

    decision = loop_decide(state, {}, iteration_n=2)

    assert decision.terminate is False
    assert decision.iteration_n == 2


def test_loop_decide_continues_for_falsy_done_values() -> None:
    """Only the boolean literal ``True`` terminates — no truthiness coercion.

    The contract in :func:`ctxr.fsm.core.loop.decide` is ``is True``, so
    any other value (including the string ``"true"``, the int ``1``,
    or the empty dict ``{}``) must keep the loop running.
    """
    state = _make_loop_state(max_iterations=10)

    falsy_values: list[Any] = [
        False,
        None,
        0,
        "",
        [],
        {},
    ]
    for value in falsy_values:
        decision = loop_decide(state, {"done": value}, iteration_n=1)
        assert decision.terminate is False, f"falsy value {value!r} should not terminate"
        assert decision.iteration_n == 1


def test_loop_decide_continues_for_non_true_truthy_values() -> None:
    """Strict ``is True`` check: even truthy non-True values do NOT terminate."""
    state = _make_loop_state(max_iterations=10)

    truthy_but_not_true: list[Any] = [
        1,
        "true",
        "yes",
        ["done"],
        {"nested": True},
    ]
    for value in truthy_but_not_true:
        decision = loop_decide(state, {"done": value}, iteration_n=1)
        assert decision.terminate is False, (
            f"non-True truthy value {value!r} must not terminate the loop"
        )


def test_loop_decide_returns_not_loop_when_state_has_no_loop() -> None:
    """Defensive contract: a non-loop state should report ``is_loop=False``."""
    state = State(id="plain", purpose="no loop here")

    decision = loop_decide(state, {"done": True}, iteration_n=1)

    assert decision.is_loop is False
    assert decision.terminate is False
    assert decision.reason is None


# ---------------------------------------------------------------------------
# advance(): loop continue returns kind="loop_continue" with incremented
# iteration_n and a brief that carries the outputs_path.
# ---------------------------------------------------------------------------


def test_advance_loop_continue_increments_iteration_and_sets_outputs_path() -> None:
    spec = _make_spec_with_loop_and_terminal(max_iterations=10)
    run_id = uuid4()
    ctx = RunCtx(
        run_id=run_id,
        fsm_id=spec.id,
        current_state="looping",
        iteration_n=1,
        env={"seed": "alpha"},
    )

    result = advance(spec, ctx, outputs={"done": False, "note": "keep going"})

    assert result.kind == "loop_continue"
    # iteration_n is the NEXT iteration (current + 1).
    assert result.iteration_n == 2
    assert result.brief is not None
    assert result.brief.iteration_n == 2
    # The brief is for the same loop state (we did not transition).
    assert result.brief.state == "looping"
    assert result.brief.has_loop is True
    # The outputs_path follows the default "<state_id>-iters" shape.
    assert result.brief.outputs_path == "workers/looping-iters/iter-2.json"
    # No transition was resolved on a continue.
    assert result.next_state is None
    assert result.evaluations == []


def test_advance_loop_continue_default_iteration_starts_at_one() -> None:
    """When ``RunCtx.iteration_n`` is None, the engine treats current as 1."""
    spec = _make_spec_with_loop_and_terminal(max_iterations=10)
    ctx = RunCtx(
        run_id=uuid4(),
        fsm_id=spec.id,
        current_state="looping",
        env={"seed": "alpha"},
    )

    result = advance(spec, ctx, outputs={"done": False})

    assert result.kind == "loop_continue"
    assert result.iteration_n == 2
    assert result.brief is not None
    assert result.brief.iteration_n == 2
    assert result.brief.outputs_path == "workers/looping-iters/iter-2.json"


def test_advance_loop_continue_honours_custom_iteration_outputs_dir() -> None:
    spec = _make_spec_with_loop_and_terminal(
        max_iterations=10,
        iteration_outputs_dir="research/iters",
    )
    ctx = RunCtx(
        run_id=uuid4(),
        fsm_id=spec.id,
        current_state="looping",
        iteration_n=2,
        env={"seed": "alpha"},
    )

    result = advance(spec, ctx, outputs={"done": False})

    assert result.kind == "loop_continue"
    assert result.iteration_n == 3
    assert result.brief is not None
    assert result.brief.outputs_path == "workers/research/iters/iter-3.json"


# ---------------------------------------------------------------------------
# advance(): loop terminate proceeds to the next state.
# ---------------------------------------------------------------------------


def test_advance_loop_terminate_done_field_proceeds_to_next_state() -> None:
    spec = _make_spec_with_loop_and_terminal(max_iterations=10)
    ctx = RunCtx(
        run_id=uuid4(),
        fsm_id=spec.id,
        current_state="looping",
        iteration_n=4,
        env={"seed": "alpha"},
    )

    result = advance(spec, ctx, outputs={"done": True, "note": "completed"})

    assert result.kind == "advance"
    assert result.next_state == "finished"
    assert result.brief is not None
    assert result.brief.state == "finished"
    # The next state is not a loop, so iteration_n / outputs_path are absent.
    assert result.brief.iteration_n is None
    assert result.brief.outputs_path is None
    # The "always" guard should have been evaluated and matched.
    assert len(result.evaluations) == 1
    assert result.evaluations[0].to == "finished"
    assert result.evaluations[0].result is True


def test_advance_loop_terminate_max_iterations_proceeds_to_next_state() -> None:
    spec = _make_spec_with_loop_and_terminal(max_iterations=3)
    ctx = RunCtx(
        run_id=uuid4(),
        fsm_id=spec.id,
        current_state="looping",
        iteration_n=3,
        env={"seed": "alpha"},
    )

    # done is still False, but iteration_n == max_iterations forces termination.
    result = advance(spec, ctx, outputs={"done": False, "note": "ran out"})

    assert result.kind == "advance"
    assert result.next_state == "finished"
    assert result.brief is not None
    assert result.brief.state == "finished"


def test_advance_loop_terminate_runs_post_validations_before_transition() -> None:
    """On termination the engine runs post-validations; a failure faults."""
    terminal = _make_terminal_state("finished")
    looping = State(
        id="looping",
        purpose="loop until done",
        loop=Loop(
            worker=Worker(
                role="iterator",
                prompt_template="iterate",
                inputs=["seed"],
                response_schema=_loop_response_schema(),
            ),
            max_iterations=5,
            done_field="done",
        ),
        # Post-validation that will fail when done == True (we expect note to equal "good").
        post_validations=[Predicate('note == "good"')],
        transitions=[Transition(to="finished", when="always")],
    )
    spec = FsmSpec(
        id="loop-spec",
        version=1,
        entry="looping",
        states=[looping, terminal],
    )
    ctx = RunCtx(
        run_id=uuid4(),
        fsm_id=spec.id,
        current_state="looping",
        iteration_n=2,
        env={"seed": "alpha"},
    )

    result = advance(spec, ctx, outputs={"done": True, "note": "bad"})

    assert result.kind == "fault"
    assert result.reason == "post_validation_failed"
    assert len(result.post_validations) == 1
    assert result.post_validations[0].result is False
