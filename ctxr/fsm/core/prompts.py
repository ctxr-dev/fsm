"""Jinja2-sandboxed prompt rendering for FSM workers (W23f).

A :class:`Worker`'s ``prompt_template`` is a free-form string today, with
the worker's ``response_schema`` declared in parallel. The two surfaces
drift trivially: a schema edit forgets the prompt copy, or the prompt
describes a field the schema doesn't validate.

This module lets a spec author embed typed references inside the prompt:

* ``{{ response_schema | typescript }}`` renders the worker's own
  JSON-schema as a TypeScript interface.
* ``{{ inputs_schema | json }}`` renders the (optional) per-worker inputs
  schema as pretty JSON.
* ``{{ spec.slug }}`` / ``{{ spec.version }}`` surface registration info.
* ``{{ state.id }}`` surfaces the current state id.
* ``{{ allowed_tools }}`` resolves to the brief's allowlist.
* ``{{ spec.model(path='dotted.X') | fields_table }}`` resolves an
  importable Pydantic model via :func:`importlib.import_module` and
  renders it through one of the registered filters.

The renderer runs inside :class:`jinja2.sandbox.SandboxedEnvironment` so
arbitrary attribute traversal and Python-eval style escapes are blocked.
Templates that do NOT contain either ``{{`` or ``{%`` skip the renderer
entirely (see :func:`needs_rendering`), so the overwhelming majority of
existing specs pay zero overhead.

The same renderer powers register-time validation: every Jinja template
in a spec is parsed (Jinja syntax errors surface immediately) and
test-rendered against an empty :class:`PromptContext` so unknown tokens
fail fast at ``fsm.register_spec`` rather than at ``fsm.get_brief``
time, when the orchestrator is already in motion.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Mapping
from typing import Any

from jinja2 import StrictUndefined, TemplateSyntaxError
from jinja2.exceptions import UndefinedError
from jinja2.sandbox import SandboxedEnvironment
from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "PromptContext",
    "PromptRenderError",
    "PromptRenderer",
    "needs_rendering",
    "render_prompt",
]


# Cheap pre-check used by the engine to skip the renderer for plain
# prompt templates. Both the opening-tag and statement-tag are caught so
# ``{% if %}`` styled templates also opt in. Keeping this fast is the
# point; expensive scans happen later, inside the renderer.
_JINJA_OPENERS: tuple[str, ...] = ("{{", "{%")


def needs_rendering(template: str) -> bool:
    """Return ``True`` when ``template`` contains a Jinja construct."""

    return any(opener in template for opener in _JINJA_OPENERS)


class PromptRenderError(Exception):
    """Structured error raised when prompt rendering or validation fails.

    Carries the offending ``state_id`` and the underlying message so the
    caller (register-time validator, engine.build_brief) can wrap it in a
    typed envelope without re-deriving the context.
    """

    def __init__(
        self,
        message: str,
        *,
        state_id: str | None = None,
        line: int | None = None,
    ) -> None:
        super().__init__(message)
        self.state_id = state_id
        self.line = line
        self.message = message

    def as_envelope(self) -> dict[str, Any]:
        """Return the structured envelope shape register-time uses."""

        envelope: dict[str, Any] = {
            "error": "prompt_template_invalid",
            "message": self.message,
        }
        if self.state_id is not None:
            envelope["state_id"] = self.state_id
        if self.line is not None:
            envelope["line"] = self.line
        return envelope


class PromptContext(BaseModel):
    """Everything a sandboxed prompt template is allowed to reference.

    The renderer surfaces this object as the ``spec``/``state`` globals
    plus a few top-level conveniences. Extra fields are forbidden so a
    typo in a spec doesn't silently no-op.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    spec_slug: str | None = None
    spec_version: int | None = None
    state_id: str | None = None
    state_kind: str | None = None
    response_schema: dict[str, Any] | None = None
    inputs_schema: dict[str, Any] | None = None
    allowed_tools: list[str] = Field(default_factory=list)
    iteration_n: int | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    model_allowlist: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def _filter_json(value: Any) -> str:
    """Render ``value`` as pretty JSON (2-space indent, sorted keys)."""

    if value is None:
        return "null"
    if isinstance(value, BaseModel):
        # model_dump_json does not sort keys; route through model_dump so
        # the BaseModel branch matches the dict branch byte-for-byte.
        return json.dumps(
            value.model_dump(mode="json"),
            indent=2,
            sort_keys=True,
            default=str,
        )
    return json.dumps(value, indent=2, sort_keys=True, default=str)


def _filter_typescript(value: Any) -> str:
    """Render a JSON-Schema-ish dict as a TypeScript interface body.

    Intentionally tiny: handles object/array/string/number/boolean/null,
    plus required-field detection and a ``description`` comment per
    field. Not a full json-schema-to-ts compiler (those exist as 200-KB
    npm packages); the goal is to keep the spec readable when copied
    into a worker prompt, not to round-trip every JSON Schema corner.
    """

    if value is None:
        return "// (no schema declared)"

    schema: Mapping[str, Any]
    if isinstance(value, BaseModel):
        schema = value.model_dump()
    elif isinstance(value, Mapping):
        schema = value
    else:
        return f"// unsupported type: {type(value).__name__}"

    # Some callers pass the wrapping ResponseSchema (which has a
    # ``schema_`` field carrying the JSON Schema). Honour both shapes.
    if "schema_" in schema and isinstance(schema["schema_"], Mapping):
        schema = schema["schema_"]
    elif "schema" in schema and isinstance(schema["schema"], Mapping):
        schema = schema["schema"]

    return _ts_render(schema, indent=0)


def _ts_render(schema: Mapping[str, Any], *, indent: int) -> str:
    type_ = schema.get("type")
    pad = "  " * indent
    if type_ == "object":
        props: Mapping[str, Any] = schema.get("properties") or {}
        required: set[str] = set(schema.get("required") or [])
        if not props:
            return "Record<string, unknown>"
        lines = ["{"]
        inner_pad = "  " * (indent + 1)
        for key, sub in props.items():
            description = sub.get("description") if isinstance(sub, Mapping) else None
            if description:
                lines.append(f"{inner_pad}/** {description} */")
            opt = "" if key in required else "?"
            sub_ts = _ts_render(sub, indent=indent + 1) if isinstance(sub, Mapping) else "unknown"
            lines.append(f"{inner_pad}{key}{opt}: {sub_ts};")
        lines.append(pad + "}")
        return "\n".join(lines)
    if type_ == "array":
        items = schema.get("items")
        inner = _ts_render(items, indent=indent) if isinstance(items, Mapping) else "unknown"
        return f"Array<{inner}>"
    if type_ == "string":
        if "enum" in schema:
            return " | ".join(json.dumps(v) for v in schema["enum"])
        return "string"
    if type_ in ("number", "integer"):
        return "number"
    if type_ == "boolean":
        return "boolean"
    if type_ == "null":
        return "null"
    if isinstance(type_, list):
        return " | ".join(_ts_render({"type": t}, indent=indent) for t in type_)
    return "unknown"


def _filter_fields_table(value: Any) -> str:
    """Render a Pydantic model class or schema dict as a Markdown table.

    Accepts either a BaseModel subclass (via the ``spec.model`` global)
    or a JSON-Schema dict (the same shape ``typescript`` consumes). Emits
    a four-column GitHub-flavoured Markdown table; useful to drop into
    prompts that need a human-readable field summary.
    """

    schema: Mapping[str, Any]
    if (isinstance(value, type) and issubclass(value, BaseModel)) or isinstance(value, BaseModel):
        schema = value.model_json_schema()
    elif isinstance(value, Mapping):
        schema = value
    else:
        return "| (no fields available) |\n| --- |"

    # Match _filter_typescript: ResponseSchema serialises to ``schema_``
    # with by_alias=False and ``schema`` with by_alias=True.
    for key in ("schema_", "schema"):
        if key in schema and isinstance(schema[key], Mapping):
            schema = schema[key]
            break

    props: Mapping[str, Any] = schema.get("properties") or {}
    required: set[str] = set(schema.get("required") or [])
    if not props:
        return "| (empty) |\n| --- |"

    lines = ["| Field | Type | Required | Description |", "| --- | --- | --- | --- |"]
    for key, sub in props.items():
        if not isinstance(sub, Mapping):
            lines.append(f"| {key} | unknown | - | - |")
            continue
        sub_type = sub.get("type", "any")
        if isinstance(sub_type, list):
            sub_type = " \\| ".join(sub_type)
        req = "yes" if key in required else "no"
        desc = (sub.get("description") or "").replace("|", "\\|")
        lines.append(f"| {key} | {sub_type} | {req} | {desc} |")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


class PromptRenderer:
    """Sandboxed renderer for FSM worker prompt templates.

    One renderer instance is cheap to construct; callers may either build
    a fresh one per render or reuse a long-lived instance. The internal
    Jinja environment is configured with :class:`StrictUndefined` so any
    unresolved token raises rather than silently rendering as the empty
    string.
    """

    def __init__(self, *, model_allowlist: tuple[str, ...] = ()) -> None:
        self._env = SandboxedEnvironment(
            undefined=StrictUndefined,
            autoescape=False,
            keep_trailing_newline=True,
        )
        self._env.filters["json"] = _filter_json
        self._env.filters["typescript"] = _filter_typescript
        self._env.filters["fields_table"] = _filter_fields_table
        self._model_allowlist = tuple(model_allowlist)

    # ------------------------------------------------------------------
    # Context construction
    # ------------------------------------------------------------------

    def _resolve_model(self, path: str) -> Any:
        if not isinstance(path, str) or not path:
            raise PromptRenderError("spec.model(path=…) requires a non-empty string")
        module_path, _, attr = path.rpartition(".")
        if not module_path or not attr:
            raise PromptRenderError(
                f"spec.model path '{path}' must be dotted (e.g. 'pkg.module.Model')"
            )
        if self._model_allowlist and not any(
            module_path == prefix or module_path.startswith(prefix + ".")
            for prefix in self._model_allowlist
        ):
            raise PromptRenderError(
                f"spec.model path '{path}' is outside the allowed import roots: "
                f"{', '.join(self._model_allowlist) or '(none)'}"
            )
        try:
            module = importlib.import_module(module_path)
        except ImportError as exc:
            raise PromptRenderError(
                f"spec.model could not import '{module_path}': {exc}"
            ) from exc
        try:
            obj = getattr(module, attr)
        except AttributeError as exc:
            raise PromptRenderError(
                f"spec.model('{path}') resolved module '{module_path}' but it has no "
                f"attribute '{attr}'"
            ) from exc
        # Without this guard spec.model() is a general-purpose import
        # primitive (e.g. ``spec.model(path='os.environ')`` leaks process
        # env). Constrain the surface to Pydantic model classes, which is
        # the only shape the registered filters know how to render.
        if not (isinstance(obj, type) and issubclass(obj, BaseModel)):
            raise PromptRenderError(
                f"spec.model('{path}') resolved to {type(obj).__name__!s}, "
                "but only pydantic BaseModel subclasses are allowed"
            )
        return obj

    def _build_jinja_context(self, context: PromptContext) -> dict[str, Any]:
        spec_ns = {
            "slug": context.spec_slug,
            "version": context.spec_version,
            "model": self._resolve_model,
        }
        state_ns = {
            "id": context.state_id,
            "kind": context.state_kind,
        }
        return {
            "spec": spec_ns,
            "state": state_ns,
            "response_schema": context.response_schema,
            "inputs_schema": context.inputs_schema,
            "allowed_tools": list(context.allowed_tools),
            "iteration_n": context.iteration_n,
            "args": dict(context.args),
            "metadata": dict(context.metadata),
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def render(self, template: str, context: PromptContext) -> str:
        """Render ``template`` against ``context`` inside the sandbox.

        Raises :class:`PromptRenderError` for Jinja syntax errors,
        unknown tokens (StrictUndefined), unsafe attribute access
        (sandbox), or model-resolution failures.
        """

        try:
            tmpl = self._env.from_string(template)
        except TemplateSyntaxError as exc:
            raise PromptRenderError(
                f"prompt template has a Jinja syntax error: {exc.message}",
                line=exc.lineno,
            ) from exc

        jinja_context = self._build_jinja_context(context)
        try:
            return tmpl.render(**jinja_context)
        except UndefinedError as exc:
            raise PromptRenderError(
                f"prompt template references an unknown variable: {exc.message}"
            ) from exc
        except PromptRenderError:
            raise
        except Exception as exc:
            raise PromptRenderError(
                f"prompt template render failed: {exc}"
            ) from exc

    def validate(self, template: str, *, state_id: str | None = None) -> None:
        """Smoke-render ``template`` with an empty context.

        Surfaces Jinja syntax errors + obvious model-resolution failures
        at register time. Templates that legitimately reference run-time
        values pass (the StrictUndefined check is per-access and an
        empty context still satisfies tokens like ``allowed_tools``,
        which default to an empty list).
        """

        try:
            self.render(template, PromptContext())
        except PromptRenderError as exc:
            # Re-raise with the state id attached so the spec-level
            # validator can roll multiple state failures into one
            # structured envelope.
            raise PromptRenderError(
                exc.message,
                state_id=state_id,
                line=exc.line,
            ) from exc


# Module-level convenience for one-off renders (most call sites build
# their own PromptRenderer so they can pin a model_allowlist).
_DEFAULT_RENDERER: PromptRenderer | None = None


def render_prompt(template: str, context: PromptContext) -> str:
    """Render ``template`` using a shared default :class:`PromptRenderer`.

    The shared instance has no ``model_allowlist`` so ``spec.model(...)``
    calls succeed against any importable module. Callers that need a
    tighter sandbox (skills, register-time validation against an
    untrusted spec) should construct their own renderer.
    """

    global _DEFAULT_RENDERER
    if _DEFAULT_RENDERER is None:
        _DEFAULT_RENDERER = PromptRenderer()
    return _DEFAULT_RENDERER.render(template, context)
