"""Unit tests for transition resolution and the ``advance`` driver.

These tests exercise the four guard shapes (``always``, ``otherwise``,
deterministic predicate, and judgement), the first-match-wins rule, the
no-match return, the terminal-state behaviour when a state declares no
transitions at all, and the discriminated ``EngineAdvanceResult`` envelope
emitted by :func:`ctxr.fsm.core.engine.advance`.

They use only the pure :mod:`ctxr.fsm.core` surface: no SQLite, no MCP,
no API. Tests construct minimal :class:`FsmSpec` instances directly and
drive :func:`advance` / :func:`resolve_transition` with literal env dicts.
"""

from __future__ import annotations

from uuid import uuid4

from ctxr.fsm.core import (
    EngineAdvanceResult,
    FsmSpec,
    Predicate,
    RunCtx,
    State,
    Transition,
    advance,
    resolve_transition,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _state(state_id: str, transitions: list[Transition]) -> State:
    """Build a worker-less, loop-less state with the given transitions.

    A worker-less state has no response schema (so ``validate_output``
    returns trivially valid) and no post-validations (so
    ``run_post_validations`` returns trivially valid). That isolates the
    test to transition-resolution behaviour.
    """
    return State(id=state_id, transitions=transitions)


def _spec(states: list[State], entry: str | None = None) -> FsmSpec:
    """Build a minimal :class:`FsmSpec` wrapping ``states``."""
    return FsmSpec(
        id="test_fsm",
        version=1,
        entry=entry if entry is not None else states[0].id,
        states=states,
    )


# ---------------------------------------------------------------------------
# resolve_transition: the four guard kinds
# ---------------------------------------------------------------------------


def test_resolve_transition_always_matches_unconditionally() -> None:
    state = _state("s", [Transition(to="next", when="always")])

    winner, evaluations = resolve_transition(state, env={})

    assert winner is not None
    assert winner.to == "next"
    assert len(evaluations) == 1
    assert evaluations[0].result is True
    assert evaluations[0].kind == "always"


def test_resolve_transition_otherwise_matches_when_no_earlier_match() -> None:
    state = _state(
        "s",
        [
            Transition(to="never", when=Predicate("x == 1")),
            Transition(to="fallback", when="otherwise"),
        ],
    )

    winner, evaluations = resolve_transition(state, env={"x": 0})

    assert winner is not None
    assert winner.to == "fallback"
    assert evaluations[0].result is False
    assert evaluations[0].kind == "deterministic"
    assert evaluations[1].result is True
    assert evaluations[1].kind == "otherwise"


def test_resolve_transition_otherwise_skipped_when_earlier_matched() -> None:
    state = _state(
        "s",
        [
            Transition(to="winner", when=Predicate("x == 1")),
            Transition(to="fallback", when="otherwise"),
        ],
    )

    winner, evaluations = resolve_transition(state, env={"x": 1})

    assert winner is not None
    assert winner.to == "winner"
    # The otherwise branch is still evaluated for trace completeness,
    # but it must evaluate to False because an earlier transition matched.
    assert evaluations[1].result is False
    assert evaluations[1].kind == "otherwise"


def test_resolve_transition_deterministic_evaluates_expression() -> None:
    state = _state(
        "s",
        [Transition(to="next", when=Predicate("x > 5"))],
    )

    winner_true, evals_true = resolve_transition(state, env={"x": 10})
    winner_false, evals_false = resolve_transition(state, env={"x": 1})

    assert winner_true is not None and winner_true.to == "next"
    assert evals_true[0].result is True
    assert evals_true[0].kind == "deterministic"
    assert evals_true[0].expression == "x > 5"

    assert winner_false is None
    assert evals_false[0].result is False
    assert evals_false[0].kind == "deterministic"


def test_resolve_transition_judgement_matches_pick() -> None:
    state = _state(
        "s",
        [
            Transition(
                to="path_a",
                when={"kind": "judgement", "criteria": "looks like A"},
            ),
            Transition(
                to="path_b",
                when={"kind": "judgement", "criteria": "looks like B"},
            ),
        ],
    )

    winner, evaluations = resolve_transition(state, env={}, judgement_pick="path_b")

    assert winner is not None
    assert winner.to == "path_b"
    assert evaluations[0].result is False
    assert evaluations[0].kind == "judgement"
    assert evaluations[1].result is True
    assert evaluations[1].kind == "judgement"
    assert evaluations[1].criteria == "looks like B"


def test_resolve_transition_judgement_with_no_pick_matches_nothing() -> None:
    state = _state(
        "s",
        [
            Transition(
                to="path_a",
                when={"kind": "judgement", "criteria": "c"},
            ),
        ],
    )

    winner, evaluations = resolve_transition(state, env={}, judgement_pick=None)

    assert winner is None
    assert evaluations[0].result is False
    assert evaluations[0].kind == "judgement"


# ---------------------------------------------------------------------------
# resolve_transition: ordering and no-match semantics
# ---------------------------------------------------------------------------


def test_resolve_transition_first_match_wins() -> None:
    """When multiple guards match, only the first in declared order wins."""
    state = _state(
        "s",
        [
            Transition(to="first", when=Predicate("x == 1")),
            Transition(to="second", when=Predicate("x == 1")),
            Transition(to="third", when="always"),
        ],
    )

    winner, evaluations = resolve_transition(state, env={"x": 1})

    assert winner is not None
    assert winner.to == "first"
    # All three evaluations should still be present in the trace.
    assert [e.to for e in evaluations] == ["first", "second", "third"]
    # The first two both evaluate True against env (forensic trace), but
    # the engine only returns the first as the winner.
    assert evaluations[0].result is True
    assert evaluations[1].result is True
    assert evaluations[2].result is True


def test_resolve_transition_no_match_returns_none() -> None:
    state = _state(
        "s",
        [
            Transition(to="a", when=Predicate("x == 1")),
            Transition(to="b", when=Predicate("x == 2")),
        ],
    )

    winner, evaluations = resolve_transition(state, env={"x": 99})

    assert winner is None
    assert len(evaluations) == 2
    assert all(e.result is False for e in evaluations)


def test_resolve_transition_empty_transitions_returns_none_cleanly() -> None:
    """A terminal state (no transitions declared) returns ``(None, [])``."""
    state = _state("s", [])

    winner, evaluations = resolve_transition(state, env={})

    assert winner is None
    assert evaluations == []


# ---------------------------------------------------------------------------
# advance(): terminal vs normal-transition envelope shape
# ---------------------------------------------------------------------------


def test_advance_emits_terminal_on_state_with_no_transitions() -> None:
    """A state with ``transitions=[]`` must yield ``kind="terminal"``."""
    terminal = _state("done", [])
    spec = _spec([terminal])
    run_id = uuid4()
    ctx = RunCtx(
        run_id=run_id,
        fsm_id=spec.id,
        current_state="done",
        env={},
    )

    result = advance(spec, ctx, outputs={"verdict": "passed"})

    assert isinstance(result, EngineAdvanceResult)
    assert result.kind == "terminal"
    assert result.verdict == "passed"
    # No outgoing transitions means no evaluations were generated.
    assert result.evaluations == []
    # Terminal results don't carry a next brief.
    assert result.brief is None
    assert result.next_state is None


def test_advance_emits_terminal_with_none_verdict_when_absent() -> None:
    """A terminal step with no ``verdict`` in env or outputs returns ``None``."""
    terminal = _state("done", [])
    spec = _spec([terminal])
    ctx = RunCtx(
        run_id=uuid4(),
        fsm_id=spec.id,
        current_state="done",
        env={},
    )

    result = advance(spec, ctx, outputs={})

    assert result.kind == "terminal"
    assert result.verdict is None


def test_advance_emits_advance_on_normal_transition_with_brief() -> None:
    """A matching transition yields ``kind="advance"`` with a populated brief.

    ``brief.has_worker`` must be ``False`` for a worker-less next state
    and ``True`` for a state that declares a worker.
    """
    # Next state has no worker -> has_worker should be False on its brief.
    next_no_worker = _state("next", [])
    src = _state("src", [Transition(to="next", when="always")])
    spec = _spec([src, next_no_worker], entry="src")

    run_id = uuid4()
    ctx = RunCtx(
        run_id=run_id,
        fsm_id=spec.id,
        current_state="src",
        env={},
    )

    result = advance(spec, ctx, outputs={})

    assert result.kind == "advance"
    assert result.next_state == "next"
    assert result.brief is not None
    assert result.brief.state == "next"
    assert result.brief.run_id == run_id
    assert result.brief.fsm_id == spec.id
    assert result.brief.has_worker is False
    assert result.brief.has_loop is False
    # The transition evaluations trace must include the matching guard.
    assert len(result.evaluations) == 1
    assert result.evaluations[0].to == "next"
    assert result.evaluations[0].result is True


def test_advance_advance_brief_has_worker_true_when_next_state_has_worker() -> None:
    """``brief.has_worker`` is ``True`` when the next state declares a worker."""
    from ctxr.fsm.core import Worker

    worker = Worker(
        role="next-worker",
        prompt_template="do the thing",
        inputs=["payload"],
    )
    next_with_worker = State(id="next", worker=worker)
    src = _state("src", [Transition(to="next", when="always")])
    spec = _spec([src, next_with_worker], entry="src")

    ctx = RunCtx(
        run_id=uuid4(),
        fsm_id=spec.id,
        current_state="src",
        env={"payload": "hello"},
    )

    result = advance(spec, ctx, outputs={})

    assert result.kind == "advance"
    assert result.next_state == "next"
    assert result.brief is not None
    assert result.brief.has_worker is True
    assert result.brief.has_loop is False
    # Inputs are resolved from the merged env (run env + outputs).
    assert result.brief.inputs == {"payload": "hello"}


def test_advance_fault_when_transitions_declared_but_none_match() -> None:
    """Declared transitions with no match yields ``kind="fault"``."""
    src = _state("src", [Transition(to="next", when=Predicate("x == 1"))])
    nxt = _state("next", [])
    spec = _spec([src, nxt], entry="src")

    ctx = RunCtx(
        run_id=uuid4(),
        fsm_id=spec.id,
        current_state="src",
        env={"x": 99},
    )

    result = advance(spec, ctx, outputs={})

    assert result.kind == "fault"
    assert result.reason == "no_transition_matched"
    assert len(result.evaluations) == 1
    assert result.evaluations[0].result is False
