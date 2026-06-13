"""Tests for the W23f Jinja2-sandboxed prompt renderer.

Covers: each registered filter, each context surface (response_schema /
inputs_schema / state / spec / allowed_tools / args / metadata), the
StrictUndefined contract, model-resolution success + failure, the
allowlist enforcement, and the smoke-render `validate` entry point.
"""

from __future__ import annotations

import pytest

from ctxr.fsm.core.prompts import (
    PromptContext,
    PromptRenderer,
    PromptRenderError,
    needs_rendering,
    render_prompt,
)

# ---------------------------------------------------------------------------
# Cheap pre-check
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("plain prompt with no markers", False),
        ("references {{ response_schema }}", True),
        ("uses {% if allowed_tools %}control flow{% endif %}", True),
        ("escaped \\{\\{ not a token \\}\\}", False),
    ],
)
def test_needs_rendering_detects_jinja_markers(template: str, expected: bool) -> None:
    assert needs_rendering(template) is expected


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def test_json_filter_pretty_prints_with_sorted_keys() -> None:
    template = "{{ response_schema | json }}"
    schema = {"b": 2, "a": 1}
    out = render_prompt(template, PromptContext(response_schema=schema))
    # Sorted keys means 'a' comes before 'b' textually.
    assert out.strip().startswith('{\n  "a": 1,')


def test_json_filter_renders_none_as_null() -> None:
    template = "{{ response_schema | json }}"
    out = render_prompt(template, PromptContext(response_schema=None))
    assert out.strip() == "null"


def test_typescript_filter_renders_object_schema() -> None:
    schema = {
        "type": "object",
        "required": ["verdict"],
        "properties": {
            "verdict": {"type": "string", "enum": ["GO", "NO-GO"]},
            "notes": {"type": "string", "description": "free-form rationale"},
        },
    }
    template = "{{ response_schema | typescript }}"
    out = render_prompt(template, PromptContext(response_schema=schema))
    assert 'verdict: "GO" | "NO-GO"' in out
    assert "notes?: string" in out
    assert "/** free-form rationale */" in out


def test_typescript_filter_renders_array_of_objects() -> None:
    schema = {
        "type": "array",
        "items": {
            "type": "object",
            "required": ["id"],
            "properties": {
                "id": {"type": "string"},
                "count": {"type": "integer"},
            },
        },
    }
    out = render_prompt(
        "{{ response_schema | typescript }}",
        PromptContext(response_schema=schema),
    )
    assert out.strip().startswith("Array<")
    assert "id: string;" in out
    assert "count?: number;" in out


def test_typescript_filter_unwraps_response_schema_wrapper() -> None:
    # ResponseSchema serialises to {"schema_": {...}} with by_alias=False
    # and {"schema": {...}} with by_alias=True. Both must be handled.
    inner = {
        "type": "object",
        "required": ["ok"],
        "properties": {"ok": {"type": "boolean"}},
    }
    for key in ("schema_", "schema"):
        out = render_prompt(
            "{{ response_schema | typescript }}",
            PromptContext(response_schema={key: inner}),
        )
        assert "ok: boolean;" in out


def test_spec_model_rejects_colon_delimited_path() -> None:
    # spec.model expects a fully dotted path. A colon (entry-point style)
    # is rejected because rpartition('.') leaves ``attr`` containing the
    # ':Verdict' suffix, which the renderer flags before any import.
    renderer = PromptRenderer()
    template = "{{ spec.model(path='tests.unit.core.test_prompts:Verdict') | fields_table }}"
    with pytest.raises(PromptRenderError):
        renderer.render(template, PromptContext())


def test_fields_table_filter_renders_schema_dict() -> None:
    schema = {
        "type": "object",
        "required": ["verdict"],
        "properties": {
            "verdict": {"type": "string", "description": "GO or NO-GO"},
            "count": {"type": "integer"},
        },
    }
    out = render_prompt(
        "{{ response_schema | fields_table }}",
        PromptContext(response_schema=schema),
    )
    assert "| Field | Type | Required | Description |" in out
    assert "| verdict | string | yes | GO or NO-GO |" in out
    assert "| count | integer | no |  |" in out


def test_fields_table_filter_handles_empty_schema() -> None:
    out = render_prompt(
        "{{ response_schema | fields_table }}",
        PromptContext(response_schema={"type": "object", "properties": {}}),
    )
    assert "(empty)" in out


# ---------------------------------------------------------------------------
# Context surfacing
# ---------------------------------------------------------------------------


def test_context_surfaces_state_and_spec_metadata() -> None:
    template = (
        "Spec: {{ spec.slug }}@{{ spec.version }} | "
        "State: {{ state.id }} ({{ state.kind }})"
    )
    out = render_prompt(
        template,
        PromptContext(
            spec_slug="code-review",
            spec_version=3,
            state_id="qa",
            state_kind="worker",
        ),
    )
    assert out.strip() == "Spec: code-review@3 | State: qa (worker)"


def test_context_surfaces_allowed_tools_via_for_loop() -> None:
    template = (
        "{% for tool in allowed_tools %}- {{ tool }}\n{% endfor %}"
    )
    out = render_prompt(
        template,
        PromptContext(allowed_tools=["Bash", "Read", "Edit"]),
    )
    assert out == "- Bash\n- Read\n- Edit\n"


def test_context_surfaces_args_and_metadata() -> None:
    template = "args.base={{ args.base }} meta.actor={{ metadata.actor }}"
    out = render_prompt(
        template,
        PromptContext(args={"base": "main"}, metadata={"actor": "alice"}),
    )
    assert out == "args.base=main meta.actor=alice"


# ---------------------------------------------------------------------------
# StrictUndefined contract
# ---------------------------------------------------------------------------


def test_unknown_token_raises_prompt_render_error() -> None:
    with pytest.raises(PromptRenderError) as info:
        render_prompt("{{ nonsense }}", PromptContext())
    assert "unknown variable" in str(info.value).lower()


def test_jinja_syntax_error_raises_with_line_number() -> None:
    with pytest.raises(PromptRenderError) as info:
        render_prompt("{{ unclosed", PromptContext())
    assert info.value.line is not None
    assert "syntax" in str(info.value).lower()


# ---------------------------------------------------------------------------
# spec.model resolution
# ---------------------------------------------------------------------------


def test_spec_model_resolves_importable_pydantic_model() -> None:
    template = (
        "{{ spec.model(path='ctxr.fsm.core.prompts.PromptContext') | fields_table }}"
    )
    out = render_prompt(template, PromptContext())
    # PromptContext declares spec_slug among its fields; the rendered
    # table proves the import + schema-extraction path is healthy.
    assert "spec_slug" in out


def test_spec_model_rejects_non_dotted_path() -> None:
    with pytest.raises(PromptRenderError) as info:
        render_prompt(
            "{{ spec.model(path='BaseModel') }}",
            PromptContext(),
        )
    assert "must be dotted" in str(info.value)


def test_spec_model_reports_missing_attribute() -> None:
    with pytest.raises(PromptRenderError) as info:
        render_prompt(
            "{{ spec.model(path='pydantic.NotAClass') | json }}",
            PromptContext(),
        )
    assert "no attribute" in str(info.value)


def test_spec_model_respects_allowlist() -> None:
    renderer = PromptRenderer(model_allowlist=("ctxr.fsm",))
    # Inside the allowlist: OK (pydantic.BaseModel is not).
    with pytest.raises(PromptRenderError) as info:
        renderer.render(
            "{{ spec.model(path='pydantic.BaseModel') }}",
            PromptContext(),
        )
    assert "outside the allowed import roots" in str(info.value)

    # Allowed prefix resolves (ctxr.fsm.core.prompts:PromptContext).
    out = renderer.render(
        "{{ spec.model(path='ctxr.fsm.core.prompts.PromptContext') | fields_table }}",
        PromptContext(),
    )
    assert "| Field | Type | Required | Description |" in out


# ---------------------------------------------------------------------------
# Sandbox safety
# ---------------------------------------------------------------------------


def test_sandbox_blocks_dunder_attribute_traversal() -> None:
    # The renderer wraps user-supplied dicts in plain Python objects, so
    # there's no path to __class__ via the registered globals; the
    # SandboxedEnvironment also blocks attribute access to dunders even
    # when the surface is reachable.
    template = "{{ args.__class__.__mro__ }}"
    with pytest.raises(PromptRenderError):
        render_prompt(template, PromptContext(args={"key": "value"}))


# ---------------------------------------------------------------------------
# validate() smoke render
# ---------------------------------------------------------------------------


def test_validate_passes_for_well_formed_template() -> None:
    renderer = PromptRenderer()
    # No exception means the smoke render against an empty context
    # succeeded. Note allowed_tools / args / metadata default to empty
    # containers so a template referencing them is still valid.
    renderer.validate(
        "Tools: {% for t in allowed_tools %}{{ t }}{% endfor %}",
        state_id="qa",
    )


def test_validate_attaches_state_id_on_failure() -> None:
    renderer = PromptRenderer()
    with pytest.raises(PromptRenderError) as info:
        renderer.validate("{{ unknown_token }}", state_id="qa")
    assert info.value.state_id == "qa"
    envelope = info.value.as_envelope()
    assert envelope["error"] == "prompt_template_invalid"
    assert envelope["state_id"] == "qa"


# ---------------------------------------------------------------------------
# Parsed-template cache: reuse the compiled template, render per context (#93)
# ---------------------------------------------------------------------------


def test_render_caches_parsed_template_but_renders_per_context() -> None:
    # The renderer caches the PARSED template keyed on template text, so a
    # second render of the same template reuses the compiled object rather
    # than re-parsing. The rendered OUTPUT, however, must always reflect
    # the live context: the same template renders different text for
    # different args.
    renderer = PromptRenderer()
    template = "Iteration {{ iteration_n }} for {{ args.who }}."

    first = renderer.render(
        template,
        PromptContext(iteration_n=1, args={"who": "alpha"}),
    )
    cached_template = renderer._template_cache[template]

    second = renderer.render(
        template,
        PromptContext(iteration_n=2, args={"who": "bravo"}),
    )

    assert first == "Iteration 1 for alpha."
    assert second == "Iteration 2 for bravo."
    # Same compiled template object reused across both renders (no re-parse).
    assert renderer._template_cache[template] is cached_template
