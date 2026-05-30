"""Tests for :func:`ctxr.fsm.core.engine.execute_inline` (W14a).

Covers:

* Happy path: a registered handler returns a schema-valid dict and the
  engine reports ``ok=True`` with the outputs.
* Unregistered handler reports ``ok=False`` with
  ``fault_reason="inline_handler_unregistered: ..."``.
* Handler that raises is caught and reported as
  ``fault_reason="inline_handler_raised: ..."``; the engine does not
  re-raise.
* Handler that returns a non-dict reports ``fault_reason="inline_handler_bad_return_type: ..."``.
* Handler returns a dict that fails the inline state's response schema
  → ``fault_reason="inline_handler_validation_failed"`` and the
  validation errors are surfaced.
* Handler returns a schema-valid dict but a post-validation predicate
  evaluates ``False`` → ``fault_reason="inline_handler_post_validation_failed"``.
* Multiple-handler registry: looking up a handler under one spec does
  not collide with the same handler_id under another spec.
* Schema-less inline state: the engine accepts ANY dict and reports
  ``ok=True``.
* Inline state without an InlineSpec (defensive): ``execute_inline``
  raises ``TypeError`` because the call site is a programming error.
* ``execute_inline`` defaults to the module-level registry when none
  is passed explicitly.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from ctxr.fsm.core.engine import execute_inline
from ctxr.fsm.core.inline_registry import (
    InlineContext,
    InlineHandlerRegistry,
    get_default_registry,
)
from ctxr.fsm.core.models import (
    InlineSpec,
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


def _verdict_schema() -> ResponseSchema:
    """Build a tiny schema declaring a required ``verdict`` boolean."""
    return ResponseSchema(
        schema={
            "type": "object",
            "properties": {
                "verdict": {"type": "boolean"},
                "note": {"type": "string"},
            },
            "required": ["verdict"],
        }
    )


def _ctx(fsm_id: str = "spec-a", current_state: str = "s1") -> RunCtx:
    return RunCtx(
        run_id=uuid4(),
        fsm_id=fsm_id,
        current_state=current_state,
    )


def _inline_state(
    state_id: str = "s1",
    handler_id: str = "h1",
    response_schema: ResponseSchema | None = None,
    post_validations: list[Predicate] | None = None,
    transitions: list[Transition] | None = None,
) -> State:
    spec = InlineSpec(
        handler_id=handler_id,
        response_schema=response_schema,
        post_validations=list(post_validations or []),
    )
    return State(
        id=state_id,
        inline=spec,
        transitions=list(transitions or []),
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_execute_inline_happy_path_runs_handler_and_validates() -> None:
    registry = InlineHandlerRegistry()

    def handler(ctx: InlineContext) -> dict[str, Any]:
        # Echo the args back into the outputs as a smoke check.
        return {"verdict": True, "note": "ok", "args_seen": ctx.args}

    registry.register("spec-a", "h1", handler)
    state = _inline_state(handler_id="h1", response_schema=_verdict_schema())
    ctx = _ctx(fsm_id="spec-a")

    result = execute_inline(
        state=state,
        ctx=ctx,
        args={"goal": "do it"},
        inputs={"x": 1},
        registry=registry,
    )

    assert result.ok is True
    assert result.handler_id == "h1"
    assert result.outputs["verdict"] is True
    assert result.outputs["args_seen"] == {"goal": "do it"}
    assert result.validation.valid is True
    assert result.validation.errors == []
    assert result.post_validations is None
    assert result.fault_reason is None


def test_execute_inline_passes_inputs_into_handler() -> None:
    registry = InlineHandlerRegistry()
    seen: dict[str, Any] = {}

    def handler(ctx: InlineContext) -> dict[str, Any]:
        seen["inputs"] = dict(ctx.inputs)
        seen["state_id"] = ctx.state_id
        seen["fsm_id"] = ctx.fsm_id
        seen["iteration_n"] = ctx.iteration_n
        return {"verdict": True}

    registry.register("spec-a", "h1", handler)
    state = _inline_state(handler_id="h1", response_schema=_verdict_schema())
    ctx = _ctx(fsm_id="spec-a", current_state="s1")

    execute_inline(
        state=state,
        ctx=ctx,
        args={},
        inputs={"a": "alpha", "b": 99},
        registry=registry,
    )

    assert seen["inputs"] == {"a": "alpha", "b": 99}
    assert seen["state_id"] == "s1"
    assert seen["fsm_id"] == "spec-a"
    assert seen["iteration_n"] is None


# ---------------------------------------------------------------------------
# Unregistered handler
# ---------------------------------------------------------------------------


def test_execute_inline_unregistered_handler_returns_structured_fault() -> None:
    registry = InlineHandlerRegistry()
    state = _inline_state(handler_id="missing_one", response_schema=_verdict_schema())
    ctx = _ctx(fsm_id="spec-x")

    result = execute_inline(
        state=state,
        ctx=ctx,
        args={},
        inputs={},
        registry=registry,
    )

    assert result.ok is False
    assert result.fault_reason is not None
    assert result.fault_reason.startswith("inline_handler_unregistered:")
    assert "spec-x" in result.fault_reason
    assert "missing_one" in result.fault_reason
    assert result.outputs == {}


# ---------------------------------------------------------------------------
# Handler raises
# ---------------------------------------------------------------------------


def test_execute_inline_raising_handler_is_caught_and_reported() -> None:
    registry = InlineHandlerRegistry()

    def handler(ctx: InlineContext) -> dict[str, Any]:
        raise RuntimeError("boom")

    registry.register("spec-a", "h1", handler)
    state = _inline_state(handler_id="h1", response_schema=_verdict_schema())
    ctx = _ctx(fsm_id="spec-a")

    result = execute_inline(
        state=state,
        ctx=ctx,
        args={},
        inputs={},
        registry=registry,
    )

    assert result.ok is False
    assert result.fault_reason is not None
    assert result.fault_reason.startswith("inline_handler_raised:")
    assert "RuntimeError" in result.fault_reason
    assert "boom" in result.fault_reason
    # Outputs are empty; the engine does not silently swallow partial data.
    assert result.outputs == {}


# ---------------------------------------------------------------------------
# Handler returns non-dict
# ---------------------------------------------------------------------------


def test_execute_inline_bad_return_type_is_reported() -> None:
    registry = InlineHandlerRegistry()

    def handler(ctx: InlineContext) -> dict[str, Any]:
        return ["not", "a", "dict"]  # type: ignore[return-value]

    registry.register("spec-a", "h1", handler)
    state = _inline_state(handler_id="h1", response_schema=_verdict_schema())
    ctx = _ctx(fsm_id="spec-a")

    result = execute_inline(
        state=state,
        ctx=ctx,
        args={},
        inputs={},
        registry=registry,
    )

    assert result.ok is False
    assert result.fault_reason is not None
    assert result.fault_reason.startswith("inline_handler_bad_return_type:")
    assert "list" in result.fault_reason
    assert result.outputs == {}


# ---------------------------------------------------------------------------
# Schema validation failure
# ---------------------------------------------------------------------------


def test_execute_inline_schema_mismatch_is_reported() -> None:
    registry = InlineHandlerRegistry()

    def handler(ctx: InlineContext) -> dict[str, Any]:
        # ``verdict`` is required + must be boolean; this returns int.
        return {"verdict": 7}

    registry.register("spec-a", "h1", handler)
    state = _inline_state(handler_id="h1", response_schema=_verdict_schema())
    ctx = _ctx(fsm_id="spec-a")

    result = execute_inline(
        state=state,
        ctx=ctx,
        args={},
        inputs={},
        registry=registry,
    )

    assert result.ok is False
    assert result.fault_reason == "inline_handler_validation_failed"
    assert result.validation.valid is False
    assert result.validation.errors  # at least one error message present
    assert result.outputs == {}


# ---------------------------------------------------------------------------
# Post-validation failure
# ---------------------------------------------------------------------------


def test_execute_inline_post_validation_failure_is_reported() -> None:
    registry = InlineHandlerRegistry()

    def handler(ctx: InlineContext) -> dict[str, Any]:
        return {"verdict": False, "note": "bad"}

    registry.register("spec-a", "h1", handler)
    # Post-validation asserts ``verdict == True``; the handler returns False.
    state = _inline_state(
        handler_id="h1",
        response_schema=_verdict_schema(),
        post_validations=[Predicate("verdict == true")],
    )
    ctx = _ctx(fsm_id="spec-a")

    result = execute_inline(
        state=state,
        ctx=ctx,
        args={},
        inputs={},
        registry=registry,
    )

    assert result.ok is False
    assert result.fault_reason == "inline_handler_post_validation_failed"
    assert result.validation.valid is True  # the schema part was fine
    assert result.post_validations is not None
    assert result.post_validations.valid is False
    assert len(result.post_validations.results) == 1
    assert result.post_validations.results[0].result is False


def test_execute_inline_post_validation_success_clears_fault_reason() -> None:
    registry = InlineHandlerRegistry()

    def handler(ctx: InlineContext) -> dict[str, Any]:
        return {"verdict": True, "note": "ok"}

    registry.register("spec-a", "h1", handler)
    state = _inline_state(
        handler_id="h1",
        response_schema=_verdict_schema(),
        post_validations=[Predicate("verdict == true")],
    )
    ctx = _ctx(fsm_id="spec-a")

    result = execute_inline(
        state=state,
        ctx=ctx,
        args={},
        inputs={},
        registry=registry,
    )

    assert result.ok is True
    assert result.fault_reason is None
    assert result.post_validations is not None
    assert result.post_validations.valid is True


# ---------------------------------------------------------------------------
# Per-spec scoping at execute_inline level
# ---------------------------------------------------------------------------


def test_execute_inline_per_spec_scoping_resolves_correct_handler() -> None:
    """Two specs can register the same handler_id without execute_inline collision."""
    registry = InlineHandlerRegistry()

    def handler_a(ctx: InlineContext) -> dict[str, Any]:
        return {"verdict": True, "note": "from-a"}

    def handler_b(ctx: InlineContext) -> dict[str, Any]:
        return {"verdict": True, "note": "from-b"}

    registry.register("spec-a", "common", handler_a)
    registry.register("spec-b", "common", handler_b)

    state = _inline_state(handler_id="common", response_schema=_verdict_schema())

    result_a = execute_inline(
        state=state,
        ctx=_ctx(fsm_id="spec-a"),
        args={},
        inputs={},
        registry=registry,
    )
    result_b = execute_inline(
        state=state,
        ctx=_ctx(fsm_id="spec-b"),
        args={},
        inputs={},
        registry=registry,
    )

    assert result_a.ok and result_a.outputs["note"] == "from-a"
    assert result_b.ok and result_b.outputs["note"] == "from-b"


# ---------------------------------------------------------------------------
# Schema-less inline state
# ---------------------------------------------------------------------------


def test_execute_inline_schemaless_state_accepts_any_dict_output() -> None:
    """A terminal inline state without response_schema is always-valid."""
    registry = InlineHandlerRegistry()

    def handler(ctx: InlineContext) -> dict[str, Any]:
        return {"anything": "goes", "n": 42}

    registry.register("spec-a", "h1", handler)
    state = _inline_state(handler_id="h1", response_schema=None)
    ctx = _ctx(fsm_id="spec-a")

    result = execute_inline(
        state=state,
        ctx=ctx,
        args={},
        inputs={},
        registry=registry,
    )

    assert result.ok is True
    assert result.validation.valid is True
    assert result.outputs == {"anything": "goes", "n": 42}


# ---------------------------------------------------------------------------
# Defensive: execute_inline on a non-inline state
# ---------------------------------------------------------------------------


def test_execute_inline_raises_on_non_inline_state() -> None:
    """Calling execute_inline on a worker state is a programming error."""
    state = State(id="worker_state", worker=Worker(role="w", prompt_template="t"))
    with pytest.raises(TypeError, match="not an inline state"):
        execute_inline(
            state=state,
            ctx=_ctx(),
            args={},
            inputs={},
            registry=InlineHandlerRegistry(),
        )


# ---------------------------------------------------------------------------
# Default registry fallback
# ---------------------------------------------------------------------------


def test_execute_inline_defaults_to_module_level_registry_when_none_supplied() -> None:
    """Passing ``registry=None`` falls back to ``get_default_registry()``."""
    default = get_default_registry()
    spec_id = "spec-default-fallback-test"
    try:

        def handler(ctx: InlineContext) -> dict[str, Any]:
            return {"verdict": True}

        default.register(spec_id, "h1", handler)
        state = _inline_state(handler_id="h1", response_schema=_verdict_schema())
        ctx = _ctx(fsm_id=spec_id)

        result = execute_inline(
            state=state,
            ctx=ctx,
            args={},
            inputs={},
        )

        assert result.ok is True
        assert result.outputs == {"verdict": True}
    finally:
        # Keep the global registry hermetic between tests.
        default.unregister(spec_id)
