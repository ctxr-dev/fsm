"""Tests for :func:`ctxr.fsm.core.spec.validate_fsm_spec` over inline states (W14a).

Covers:

* ``validate_fsm_spec`` accepts an :class:`FsmSpec` containing inline
  states (worker → inline → terminal pattern).
* ``validate_fsm_spec`` accepts a spec with an inline state used as a
  terminal (no transitions, no response_schema needed).
* The construction-time check for "inline+transitions requires
  response_schema" prevents an invalid spec from even reaching the
  validator; we exercise that interaction at the spec-building boundary.
* Reachability over inline states is honoured (inline states are
  traversed by the BFS walk just like worker states).
* Inline handler **registration** is NOT validated at spec-load time:
  a spec referencing a handler_id with NO registered handler still
  validates cleanly. (The fault surfaces at advance time instead.)
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ctxr.fsm.core.models import (
    FsmSpec,
    InlineSpec,
    ResponseSchema,
    State,
    Transition,
    Worker,
)
from ctxr.fsm.core.spec import validate_fsm_spec

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _verdict_schema() -> ResponseSchema:
    return ResponseSchema(
        schema={
            "type": "object",
            "properties": {
                "verdict": {"type": "boolean"},
            },
            "required": ["verdict"],
        }
    )


def _simple_worker(role: str = "w") -> Worker:
    return Worker(role=role, prompt_template="do work")


# ---------------------------------------------------------------------------
# validate_fsm_spec accepts inline-bearing specs
# ---------------------------------------------------------------------------


def test_validate_accepts_worker_inline_terminal_spec() -> None:
    """Realistic worker → inline → terminal shape validates cleanly."""
    start = State(
        id="plan",
        worker=_simple_worker(role="planner"),
        transitions=[Transition(to="triage", when="always")],
    )
    triage = State(
        id="triage",
        inline=InlineSpec(
            handler_id="risk_tier_triage",
            response_schema=_verdict_schema(),
        ),
        transitions=[Transition(to="done", when="always")],
    )
    done = State(id="done")

    spec = FsmSpec(
        id="port-test",
        version=1,
        entry="plan",
        states=[start, triage, done],
    )

    result = validate_fsm_spec(spec)

    assert result.valid is True
    assert result.errors == []
    assert result.unreachable_states == []
    assert result.dangling_transitions == []
    assert result.invalid_predicates == []


def test_validate_accepts_terminal_inline_state_without_schema() -> None:
    """An inline state with no transitions and no schema is a valid terminal."""
    start = State(
        id="plan",
        worker=_simple_worker(role="planner"),
        transitions=[Transition(to="cleanup", when="always")],
    )
    cleanup = State(
        id="cleanup",
        inline=InlineSpec(handler_id="terminal_cleanup"),
    )

    spec = FsmSpec(
        id="terminal-inline",
        version=1,
        entry="plan",
        states=[start, cleanup],
    )

    result = validate_fsm_spec(spec)
    assert result.valid is True
    assert result.errors == []


def test_validate_accepts_inline_only_spec_one_state() -> None:
    """A spec whose entry is an inline terminal state validates cleanly."""
    only = State(
        id="just_inline",
        inline=InlineSpec(handler_id="solo"),
    )
    spec = FsmSpec(id="solo-spec", version=1, entry="just_inline", states=[only])

    result = validate_fsm_spec(spec)
    assert result.valid is True


# ---------------------------------------------------------------------------
# Construction-time check for inline+transitions+no-schema
# ---------------------------------------------------------------------------


def test_build_spec_with_inline_transitions_no_schema_fails_at_state_construction() -> None:
    """The State validator rejects the invalid combo before FsmSpec sees it.

    This documents the layering: ``validate_fsm_spec`` would also catch
    the violation (see ``_check_inline_semantics``), but the
    construction-time validator on :class:`State` fires first and is
    the canonical gate.
    """
    with pytest.raises(ValidationError) as excinfo:
        State(
            id="bad",
            inline=InlineSpec(handler_id="x"),
            transitions=[Transition(to="elsewhere", when="always")],
        )
    assert "inline" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Reachability over inline states
# ---------------------------------------------------------------------------


def test_validate_reachability_traverses_inline_states() -> None:
    """An inline state on the spine is reachable from entry."""
    a = State(
        id="a",
        worker=_simple_worker(role="a"),
        transitions=[Transition(to="b", when="always")],
    )
    b = State(
        id="b",
        inline=InlineSpec(
            handler_id="middle_inline",
            response_schema=_verdict_schema(),
        ),
        transitions=[Transition(to="c", when="always")],
    )
    c = State(id="c")

    spec = FsmSpec(id="spec-reach", version=1, entry="a", states=[a, b, c])
    result = validate_fsm_spec(spec)
    assert result.valid is True
    assert result.unreachable_states == []


def test_validate_reports_unreachable_inline_state() -> None:
    """An inline state nobody points to is reported as unreachable."""
    a = State(
        id="a",
        worker=_simple_worker(role="a"),
        # Note: no transition to ``orphan`` anywhere.
        transitions=[Transition(to="terminal", when="always")],
    )
    orphan = State(id="orphan", inline=InlineSpec(handler_id="never_run"))
    terminal = State(id="terminal")

    spec = FsmSpec(
        id="orphan-spec",
        version=1,
        entry="a",
        states=[a, orphan, terminal],
    )
    result = validate_fsm_spec(spec)
    assert result.valid is False
    assert "orphan" in result.unreachable_states


# ---------------------------------------------------------------------------
# Handler registration is NOT checked at spec-load time
# ---------------------------------------------------------------------------


def test_validate_does_not_require_handler_registration() -> None:
    """A spec with an inline state whose handler is NOT registered validates fine.

    The fault surfaces only at engine advance time
    (``inline_handler_unregistered``). This separation lets consumers
    register the spec and the inline handlers independently — e.g.
    ``Project.register_spec`` before ``Project.register_inline_handlers``
    is allowed, and reverse order works too.
    """
    a = State(
        id="a",
        inline=InlineSpec(handler_id="not_yet_registered_handler"),
    )
    spec = FsmSpec(id="unreg-spec", version=1, entry="a", states=[a])

    result = validate_fsm_spec(spec)
    # No reference to any runtime registry was made; only structural
    # validation ran.
    assert result.valid is True
    assert result.errors == []
