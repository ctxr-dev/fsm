"""Tests for the W23f Jinja2 prompt rendering integration inside ``build_brief``.

These tests pin the contract of the engine + prompt-renderer wiring:

* A plain prompt template (no Jinja markers) passes through verbatim and
  the renderer is never invoked, even when the template contains text
  that would be a Jinja syntax error if it were parsed.
* A template referencing ``response_schema | typescript`` renders to a
  TypeScript interface body with no literal ``{{`` left in the output.
* A template referencing ``inputs_schema`` renders the worker's
  declared inputs JSON Schema when the new ``Worker.inputs_schema``
  field is set.
* Loop states render their inner ``loop.worker.prompt_template`` too:
  the brief's ``loop.worker`` carries the rendered text, not the raw
  Jinja source.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from ctxr.fsm.core import engine as engine_mod
from ctxr.fsm.core.engine import build_brief
from ctxr.fsm.core.models import (
    FsmSpec,
    Loop,
    ResponseSchema,
    State,
    Transition,
    Worker,
)
from ctxr.fsm.core.prompts import PromptRenderError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _response_schema() -> ResponseSchema:
    """A small response schema used to verify the typescript filter."""
    return ResponseSchema.model_validate(
        {
            "schema": {
                "type": "object",
                "required": ["verdict"],
                "properties": {
                    "verdict": {
                        "type": "string",
                        "description": "GO or NO-GO",
                    },
                    "notes": {"type": "string"},
                },
            }
        }
    )


def _inputs_schema() -> ResponseSchema:
    """A small inputs schema mirroring the response-schema shape."""
    return ResponseSchema.model_validate(
        {
            "schema": {
                "type": "object",
                "required": ["base_ref"],
                "properties": {
                    "base_ref": {
                        "type": "string",
                        "description": "the branch to diff against",
                    },
                },
            }
        }
    )


def _loop_response_schema() -> ResponseSchema:
    """A loop-compatible response schema with a boolean ``done`` field."""
    return ResponseSchema.model_validate(
        {
            "schema": {
                "type": "object",
                "required": ["done"],
                "properties": {
                    "done": {"type": "boolean"},
                    "note": {"type": "string"},
                },
            }
        }
    )


def _spec_with_state(state: State) -> FsmSpec:
    return FsmSpec(id="render_spec", version=2, entry=state.id, states=[state])


@pytest.fixture(autouse=True)
def _reset_prompt_renderer() -> None:
    """Reset the engine's module-level prompt renderer between tests.

    The renderer caches PARSED templates (keyed on template text), which
    is correctness-neutral, but resetting the shared instance keeps each
    test fully isolated and lets ``monkeypatch`` swaps of
    ``PromptRenderer.render`` take effect on a fresh renderer.
    """
    engine_mod._PROMPT_RENDERER = None


# ---------------------------------------------------------------------------
# Plain templates: renderer is never invoked
# ---------------------------------------------------------------------------


def test_plain_template_passes_through_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    # A template that would raise PromptRenderError if it were rendered
    # (an unknown variable would trip StrictUndefined). The plain-text
    # body has no Jinja markers, so the renderer must NOT be called.
    plain_template = "Plain prompt: please return verdict (no Jinja here)."

    worker = Worker(
        role="reviewer",
        prompt_template=plain_template,
        inputs=[],
        response_schema=_response_schema(),
    )
    state = State(
        id="qa",
        worker=worker,
        outputs=["verdict"],
        transitions=[Transition(to="qa", when="always")],
    )
    spec = _spec_with_state(state)

    # Spy: replace the PromptRenderer.render with one that raises if
    # the engine ever reaches it for a plain template.
    def _explode(*args: object, **kwargs: object) -> str:
        raise AssertionError("renderer.render must not be called for plain templates")

    monkeypatch.setattr(
        "ctxr.fsm.core.engine.PromptRenderer.render",
        _explode,
    )

    brief = build_brief(spec, state, env={}, run_id=uuid4())

    assert brief.worker is not None
    assert brief.worker.prompt_template == plain_template


def test_plain_template_with_double_brace_lookalike_skipped() -> None:
    # ``{{`` is the only Jinja marker we use to gate rendering. A
    # template that lacks it (even with stray braces) bypasses the
    # renderer, so a deliberately-broken Jinja-looking string still
    # round-trips when needs_rendering says "no".
    plain_template = "Single { brace } only, no Jinja"
    worker = Worker(
        role="reviewer",
        prompt_template=plain_template,
        inputs=[],
        response_schema=_response_schema(),
    )
    state = State(
        id="qa",
        worker=worker,
        outputs=["verdict"],
        transitions=[Transition(to="qa", when="always")],
    )
    spec = _spec_with_state(state)

    brief = build_brief(spec, state, env={}, run_id=uuid4())
    assert brief.worker is not None
    assert brief.worker.prompt_template == plain_template


# ---------------------------------------------------------------------------
# response_schema | typescript renders the TS interface body
# ---------------------------------------------------------------------------


def test_response_schema_typescript_filter_renders() -> None:
    template = (
        "Schema:\n"
        "```ts\n"
        "interface Out {{ response_schema | typescript }}\n"
        "```"
    )
    worker = Worker(
        role="reviewer",
        prompt_template=template,
        inputs=[],
        response_schema=_response_schema(),
    )
    state = State(
        id="qa",
        worker=worker,
        outputs=["verdict"],
        transitions=[Transition(to="qa", when="always")],
    )
    spec = _spec_with_state(state)

    brief = build_brief(spec, state, env={}, run_id=uuid4())

    assert brief.worker is not None
    rendered = brief.worker.prompt_template
    # The TS interface body must be present and the Jinja markers gone.
    assert "verdict: string;" in rendered
    assert "/** GO or NO-GO */" in rendered
    assert "{{" not in rendered
    assert "}}" not in rendered


# ---------------------------------------------------------------------------
# inputs_schema renders correctly when Worker.inputs_schema is set
# ---------------------------------------------------------------------------


def test_inputs_schema_renders_through_typescript_filter() -> None:
    template = (
        "Inputs:\n"
        "```ts\n"
        "interface In {{ inputs_schema | typescript }}\n"
        "```"
    )
    worker = Worker(
        role="reviewer",
        prompt_template=template,
        inputs=["base_ref"],
        response_schema=_response_schema(),
        inputs_schema=_inputs_schema(),
    )
    state = State(
        id="qa",
        worker=worker,
        outputs=["verdict"],
        transitions=[Transition(to="qa", when="always")],
    )
    spec = _spec_with_state(state)

    brief = build_brief(spec, state, env={"base_ref": "main"}, run_id=uuid4())

    assert brief.worker is not None
    rendered = brief.worker.prompt_template
    assert "base_ref: string;" in rendered
    assert "/** the branch to diff against */" in rendered
    assert "{{" not in rendered


def test_inputs_schema_absent_renders_as_no_schema_comment() -> None:
    # When inputs_schema is not declared, the typescript filter emits a
    # ``// (no schema declared)`` sentinel rather than raising.
    template = "Inputs:\n{{ inputs_schema | typescript }}"
    worker = Worker(
        role="reviewer",
        prompt_template=template,
        inputs=[],
        response_schema=_response_schema(),
    )
    state = State(
        id="qa",
        worker=worker,
        outputs=["verdict"],
        transitions=[Transition(to="qa", when="always")],
    )
    spec = _spec_with_state(state)

    brief = build_brief(spec, state, env={}, run_id=uuid4())
    assert brief.worker is not None
    assert "(no schema declared)" in brief.worker.prompt_template


# ---------------------------------------------------------------------------
# Loop state prompts also render
# ---------------------------------------------------------------------------


def test_loop_worker_prompt_template_is_rendered() -> None:
    template = (
        "Loop iteration {{ iteration_n }} of state {{ state.id }}.\n"
        "Return shape:\n{{ response_schema | typescript }}"
    )
    loop_worker = Worker(
        role="iterator",
        prompt_template=template,
        inputs=["seed"],
        response_schema=_loop_response_schema(),
    )
    loop = Loop(
        worker=loop_worker,
        max_iterations=3,
        done_field="done",
    )
    state = State(
        id="looping",
        loop=loop,
        outputs=["done"],
        transitions=[Transition(to="looping", when="always")],
    )
    spec = _spec_with_state(state)

    brief = build_brief(
        spec,
        state,
        env={"seed": "hello"},
        run_id=uuid4(),
        iteration_n=2,
    )

    # Both surfaces (top-level worker + nested loop.worker) carry the
    # rendered text. Neither should still contain the raw Jinja markers.
    assert brief.worker is not None
    assert brief.loop is not None
    assert "Loop iteration 2 of state looping." in brief.worker.prompt_template
    assert "done: boolean;" in brief.worker.prompt_template
    assert "{{" not in brief.worker.prompt_template

    assert "Loop iteration 2 of state looping." in brief.loop.worker.prompt_template
    assert "{{" not in brief.loop.worker.prompt_template


# ---------------------------------------------------------------------------
# Render cache must NOT bleed args / iteration_n across calls (#93)
# ---------------------------------------------------------------------------


def test_args_from_run_a_do_not_appear_in_run_b_prompt() -> None:
    # Regression for #93: the engine previously cached the *rendered*
    # string keyed only on (spec.id, state.id, template), so the first
    # run's args bled into every later run that re-entered the same state
    # with the same template. Render afresh per call: run B's prompt must
    # carry run B's arg, never run A's.
    template = "Diff against {{ args.base_ref }}."
    worker = Worker(
        role="reviewer",
        prompt_template=template,
        inputs=["base_ref"],
        response_schema=_response_schema(),
    )
    state = State(
        id="qa",
        worker=worker,
        outputs=["verdict"],
        transitions=[Transition(to="qa", when="always")],
    )
    spec = _spec_with_state(state)

    brief_a = build_brief(spec, state, env={"base_ref": "alpha"}, run_id=uuid4())
    brief_b = build_brief(spec, state, env={"base_ref": "bravo"}, run_id=uuid4())

    assert brief_a.worker is not None
    assert brief_b.worker is not None
    assert brief_a.worker.prompt_template == "Diff against alpha."
    # The bug would make this "Diff against alpha." (run A's arg reused).
    assert brief_b.worker.prompt_template == "Diff against bravo."
    assert "alpha" not in brief_b.worker.prompt_template


def test_distinct_iteration_n_yields_distinct_text() -> None:
    # Regression for #93: a loop body re-entered for iteration 2 must not
    # reuse iteration 1's rendered text. The cache keyed on the loop
    # state's id (shared across iterations) made every iteration reuse
    # the first iteration's prompt.
    template = "Loop iteration {{ iteration_n }} of state {{ state.id }}."
    loop_worker = Worker(
        role="iterator",
        prompt_template=template,
        inputs=["seed"],
        response_schema=_loop_response_schema(),
    )
    loop = Loop(worker=loop_worker, max_iterations=5, done_field="done")
    state = State(
        id="looping",
        loop=loop,
        outputs=["done"],
        transitions=[Transition(to="looping", when="always")],
    )
    spec = _spec_with_state(state)

    brief_iter1 = build_brief(
        spec, state, env={"seed": "x"}, run_id=uuid4(), iteration_n=1
    )
    brief_iter2 = build_brief(
        spec, state, env={"seed": "x"}, run_id=uuid4(), iteration_n=2
    )

    assert brief_iter1.worker is not None
    assert brief_iter2.worker is not None
    assert brief_iter1.worker.prompt_template == "Loop iteration 1 of state looping."
    # The bug would make this "Loop iteration 1 ..." (iter 1 text reused).
    assert brief_iter2.worker.prompt_template == "Loop iteration 2 of state looping."
    assert brief_iter1.worker.prompt_template != brief_iter2.worker.prompt_template


# ---------------------------------------------------------------------------
# Render failures surface as PromptRenderError (sanity)
# ---------------------------------------------------------------------------


def test_unknown_jinja_variable_raises_prompt_render_error() -> None:
    # When the template references a token that is not in the context,
    # StrictUndefined fires and the renderer raises. The engine
    # deliberately lets this surface (register-time validation is the
    # right place to catch it; at build_brief time it indicates a spec
    # that slipped past validation, so we want the loud failure).
    worker = Worker(
        role="reviewer",
        prompt_template="{{ nonsense_token }}",
        inputs=[],
        response_schema=_response_schema(),
    )
    state = State(
        id="qa",
        worker=worker,
        outputs=["verdict"],
        transitions=[Transition(to="qa", when="always")],
    )
    spec = _spec_with_state(state)

    with pytest.raises(PromptRenderError):
        build_brief(spec, state, env={}, run_id=uuid4())
