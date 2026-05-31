"""Tests for :class:`InlineSpec` and the ``State.kind`` derived property (W14a).

Covers:

* :class:`InlineSpec` field validation (``handler_id`` shape).
* :attr:`State.kind` property correctness across the four state kinds
  (worker / loop / inline / terminal).
* :attr:`State.kind` raising on a structurally-impossible terminal-by-
  content state that still declares transitions.
* State construction rejecting incompatible body combinations:
  ``inline`` + ``worker``, ``inline`` + ``loop``.
* State construction rejecting an inline state with transitions but no
  ``inline.response_schema`` (transition guards have no shape to read).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ctxr.fsm.core.models import (
    InlineSpec,
    Loop,
    ResponseSchema,
    State,
    Transition,
    Worker,
)

# ---------------------------------------------------------------------------
# Helpers (inline; same convention as test_models.py)
# ---------------------------------------------------------------------------


def _bool_schema(field: str = "done") -> ResponseSchema:
    """Build a tiny ResponseSchema declaring ``field`` as boolean."""
    return ResponseSchema(
        schema={
            "type": "object",
            "properties": {field: {"type": "boolean"}},
            "required": [field],
        }
    )


def _simple_worker(role: str = "worker") -> Worker:
    """Minimal worker with a non-empty role + prompt."""
    return Worker(role=role, prompt_template="do the thing")


# ---------------------------------------------------------------------------
# InlineSpec field validation
# ---------------------------------------------------------------------------


def test_inline_spec_accepts_minimal_handler_id() -> None:
    spec = InlineSpec(handler_id="risk_tier_triage")
    assert spec.handler_id == "risk_tier_triage"
    assert spec.response_schema is None
    assert spec.post_validations == []
    assert spec.purpose == ""


def test_inline_spec_accepts_full_population() -> None:
    spec = InlineSpec(
        handler_id="compute_risk",
        response_schema=_bool_schema("verdict_ok"),
        post_validations=[],
        purpose="compute risk tier from changed files",
    )
    assert spec.handler_id == "compute_risk"
    assert spec.response_schema is not None
    assert spec.purpose == "compute risk tier from changed files"


@pytest.mark.parametrize(
    "bad_id",
    [
        "BadHandler",
        "1starts_with_digit",
        "has-dash",
        "has.dot",
        "",
        "UPPER",
        "has space",
    ],
)
def test_inline_spec_rejects_bad_handler_id_shape(bad_id: str) -> None:
    with pytest.raises(ValidationError):
        InlineSpec(handler_id=bad_id)


@pytest.mark.parametrize(
    "good_id",
    ["a", "h1", "risk_tier_triage", "collect_findings", "x_y_z_123"],
)
def test_inline_spec_accepts_snake_case_handler_id(good_id: str) -> None:
    spec = InlineSpec(handler_id=good_id)
    assert spec.handler_id == good_id


# ---------------------------------------------------------------------------
# State.kind derived property — four-way correctness
# ---------------------------------------------------------------------------


def test_state_kind_returns_worker_for_worker_state() -> None:
    state = State(id="plan", worker=_simple_worker())
    assert state.kind == "worker"


def test_state_kind_returns_loop_for_loop_state() -> None:
    worker = Worker(
        role="iter",
        prompt_template="iterate",
        response_schema=_bool_schema("done"),
    )
    state = State(id="iterating", loop=Loop(worker=worker, done_field="done"))
    assert state.kind == "loop"


def test_state_kind_returns_inline_for_inline_state() -> None:
    state = State(id="triage", inline=InlineSpec(handler_id="risk_tier_triage"))
    assert state.kind == "inline"


def test_state_kind_returns_terminal_for_bare_state() -> None:
    state = State(id="done")
    assert state.kind == "terminal"


def test_state_kind_loop_wins_over_other_for_unambiguous_states() -> None:
    """A loop state's kind reports as 'loop' regardless of transitions."""
    worker = Worker(
        role="iter",
        prompt_template="iterate",
        response_schema=_bool_schema("done"),
    )
    state = State(
        id="iterating",
        loop=Loop(worker=worker, done_field="done"),
        transitions=[Transition(to="next_state", when="always")],
    )
    assert state.kind == "loop"


# ---------------------------------------------------------------------------
# State.kind raises on the structurally-impossible terminal-with-transitions
# ---------------------------------------------------------------------------


def test_state_kind_raises_when_terminal_by_content_has_transitions() -> None:
    """A state with no body and transitions is structurally invalid.

    Pydantic does not reject this at construction (a terminal "passes
    through" might be desirable in some hypothetical future), but
    ``State.kind`` is the read-time gate: a caller trying to dispatch
    on kind hits a clear error rather than silently treating the state
    as terminal.
    """
    state = State(
        id="passthrough",
        transitions=[Transition(to="elsewhere", when="always")],
    )
    with pytest.raises(ValueError, match="terminal-by-content"):
        _ = state.kind


# ---------------------------------------------------------------------------
# State construction: body-combination rejections
# ---------------------------------------------------------------------------


def test_state_rejects_both_inline_and_worker() -> None:
    with pytest.raises(ValidationError) as excinfo:
        State(
            id="mixed",
            inline=InlineSpec(handler_id="some_handler"),
            worker=_simple_worker(),
        )
    msg = str(excinfo.value)
    assert "inline" in msg and "worker" in msg


def test_state_rejects_both_inline_and_loop() -> None:
    worker = Worker(
        role="iter",
        prompt_template="iterate",
        response_schema=_bool_schema("done"),
    )
    loop = Loop(worker=worker, done_field="done")
    with pytest.raises(ValidationError) as excinfo:
        State(
            id="mixed",
            inline=InlineSpec(handler_id="some_handler"),
            loop=loop,
        )
    msg = str(excinfo.value)
    assert "inline" in msg and "loop" in msg


# ---------------------------------------------------------------------------
# State construction: inline+transitions requires response_schema
# ---------------------------------------------------------------------------


def test_state_rejects_inline_with_transitions_but_no_schema() -> None:
    """Inline state with transitions must declare inline.response_schema."""
    with pytest.raises(ValidationError) as excinfo:
        State(
            id="needs_schema",
            inline=InlineSpec(handler_id="some_handler"),
            transitions=[Transition(to="next_state", when="always")],
        )
    msg = str(excinfo.value)
    assert "inline.response_schema" in msg or "inline" in msg


def test_state_accepts_inline_with_transitions_when_schema_set() -> None:
    state = State(
        id="ok_inline",
        inline=InlineSpec(
            handler_id="some_handler",
            response_schema=_bool_schema("verdict_ok"),
        ),
        transitions=[Transition(to="next_state", when="always")],
    )
    assert state.kind == "inline"
    assert state.inline is not None
    assert state.inline.response_schema is not None


def test_state_accepts_inline_terminal_without_schema() -> None:
    """An inline state with NO transitions is a valid terminal."""
    state = State(
        id="terminal_inline",
        inline=InlineSpec(handler_id="cleanup"),
    )
    assert state.kind == "inline"
    assert state.transitions == []
