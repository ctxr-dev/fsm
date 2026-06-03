"""Pure FSM runtime engine for the ctxr FSM core.

This module is the deterministic, side-effect-free heart of the FSM.
Every function here is pure: same inputs in, same outputs out, no I/O,
no clock, no globals. The engine is consumed by higher layers (the
SQLite persistence layer in W2, the loop runtime in W3+, the MCP/HTTP
surfaces above that) which thread state into and out of it.

The engine exposes four building blocks plus one high-level driver:

* :func:`build_brief`   — materialise the :class:`Brief` handed to a
  worker for a given state entry (or one iteration of a loop body).
* :func:`validate_output` — check a worker's outputs against the
  state's ``response_schema`` (if any).
* :func:`resolve_transition` — walk a state's transitions, evaluating
  each guard against the post-output environment, and return the first
  match plus a full per-transition evaluation trace.
* :func:`run_post_validations` — evaluate every post-validation
  :class:`Predicate` declared on a state and AND-compose the result.
* :func:`advance` — the top-level "do one step" driver: validates
  outputs, decides loop continuation, runs post-validations, resolves
  the outgoing transition, and returns a discriminated
  :class:`EngineAdvanceResult`.

All errors that originate inside the predicate evaluator
(:mod:`ctxr.fsm.core.predicates`) are caught and surfaced as structured
data on :class:`TransitionEvaluation` / :class:`PostValidationResultEntry`
instead of propagating — the engine never raises on a malformed guard,
it reports the fault so the caller can journal it.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ctxr.fsm.core.inline_registry import (
    InlineContext,
    InlineHandlerRegistry,
    get_default_registry,
)
from ctxr.fsm.core.loop import decide as loop_decide
from ctxr.fsm.core.loop import outputs_path_for
from ctxr.fsm.core.models import (
    Brief,
    EngineAdvanceKind,
    FsmSpec,
    Gate,
    GateBinding,
    GateSourceKind,
    InlineExecutionResult,
    InlineFaultReason,
    Loop,
    LoopDecision,
    PostValidationResult,
    PostValidationResultEntry,
    Predicate,
    ResponseSchema,
    RunCtx,
    State,
    StateKind,
    Transition,
    TransitionEvaluation,
    TransitionKind,
    ValidationResult,
    Worker,
    WorkerOutput,
)
from ctxr.fsm.core.predicates import (
    PredicateEvalError,
    PredicateParseError,
    evaluate_expression,
    validate_expression,
)
from ctxr.fsm.core.prompts import (
    PromptContext,
    PromptRenderer,
    needs_rendering,
)

__all__ = [
    "EngineAdvanceResult",
    "GateResolution",
    "GateResolutionError",
    "advance",
    "build_brief",
    "execute_inline",
    "resolve_gate",
    "resolve_transition",
    "run_post_validations",
    "validate_output",
]


# ---------------------------------------------------------------------------
# UUIDv7 helper (graceful fallback)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Prompt rendering (W23f integration)
# ---------------------------------------------------------------------------


# Cache of rendered prompt strings keyed by (spec.id, state.id, iteration?).
# The cache exists so re-entering a state (or iterating a loop) does not
# re-parse the same template. We key on the raw template text under the
# (spec, state) tuple so a spec mutation that swaps the template body
# automatically invalidates the cached entry. Loop bodies share their
# loop state's id, so per-iteration env differences are reflected by
# always recomputing the runtime context but only re-using the parsed
# Jinja template; the renderer itself owns Jinja parsing reuse via its
# SandboxedEnvironment cache, so this dict is a lightweight per-state
# guard against re-walking the needs_rendering precheck or re-allocating
# the PromptRenderer.
_PROMPT_RENDERER: PromptRenderer | None = None
_RENDER_CACHE: dict[tuple[str, str, str], str] = {}


def _get_prompt_renderer() -> PromptRenderer:
    """Return the module-level :class:`PromptRenderer` (lazy init)."""
    global _PROMPT_RENDERER
    if _PROMPT_RENDERER is None:
        _PROMPT_RENDERER = PromptRenderer()
    return _PROMPT_RENDERER


def _maybe_render_prompt(
    spec: FsmSpec,
    state: State,
    worker: Worker,
    env: dict[str, Any],
    iteration_n: int | None,
) -> Worker:
    """Return ``worker`` with a rendered ``prompt_template`` if needed.

    A worker whose template contains no Jinja markers passes through
    untouched (the renderer is never invoked). Otherwise, the template
    is rendered through the shared :class:`PromptRenderer` with a
    :class:`PromptContext` populated from the spec, state, worker, and
    runtime env. The rendered string replaces the template on a
    ``model_copy`` clone so the original spec object remains immutable.
    """
    template = worker.prompt_template
    if not needs_rendering(template):
        return worker

    cache_key = (spec.id, state.id, template)
    cached = _RENDER_CACHE.get(cache_key)
    if cached is None:
        renderer = _get_prompt_renderer()
        context = PromptContext(
            spec_slug=spec.id,
            spec_version=spec.version,
            state_id=state.id,
            state_kind=state.kind.value,
            response_schema=(
                worker.response_schema.schema_
                if worker.response_schema is not None
                else None
            ),
            inputs_schema=(
                worker.inputs_schema.schema_
                if worker.inputs_schema is not None
                else None
            ),
            allowed_tools=list(state.allowed_tools),
            iteration_n=iteration_n,
            args=dict(env),
            metadata={},
        )
        cached = renderer.render(template, context)
        _RENDER_CACHE[cache_key] = cached

    return worker.model_copy(update={"prompt_template": cached})


def _new_brief_id() -> uuid.UUID:
    """Mint a new brief id.

    Prefer time-ordered UUIDv7 via the optional ``uuid-utils`` package
    (declared in ``pyproject.toml`` but not required at import time of
    the core), falling back to a plain UUIDv4 if it is not installed.
    Either way the return type is ``uuid.UUID`` so callers do not need
    to care which backend produced it.
    """
    try:
        from uuid_utils import uuid7
    except ImportError:
        return uuid.uuid4()
    raw = uuid7()
    # uuid_utils returns its own UUID subclass; normalise to stdlib UUID
    # so the rest of the system only ever sees a single concrete type.
    return uuid.UUID(str(raw))


# ---------------------------------------------------------------------------
# EngineAdvanceResult
# ---------------------------------------------------------------------------


class EngineAdvanceResult(BaseModel):
    """The outcome of a single :func:`advance` call.

    This is a discriminated-union-like envelope keyed off
    :attr:`kind`, a :class:`~ctxr.fsm.core.models.EngineAdvanceKind`
    member:

    * :attr:`EngineAdvanceKind.loop_continue` — the state is a loop and
      the most recent iteration did not satisfy the done-field;
      ``iteration_n`` and ``brief`` are populated with the next
      iteration's parameters.
    * :attr:`EngineAdvanceKind.fault` — validation, post-validation, or
      transition resolution failed; ``reason`` plus one of ``errors``,
      ``post_validations``, ``evaluations`` carries the diagnostic.
    * :attr:`EngineAdvanceKind.terminal` — the state has no outgoing
      transitions and the run is therefore complete; ``verdict``
      carries the optional ``verdict`` field lifted from the merged
      env.
    * :attr:`EngineAdvanceKind.advance` — a transition matched;
      ``next_state`` plus ``brief`` describe where the run goes next,
      and ``evaluations`` records the full evaluation trace.

    The model is strict and extra-forbid so a mistaken kwarg surfaces
    immediately instead of being silently dropped.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    kind: EngineAdvanceKind
    brief: Brief | None = None
    iteration_n: int | None = None
    reason: str | None = None
    errors: list[str] = Field(default_factory=list)
    post_validations: list[PostValidationResultEntry] = Field(default_factory=list)
    evaluations: list[TransitionEvaluation] = Field(default_factory=list)
    next_state: str | None = None
    verdict: Any = None


# ---------------------------------------------------------------------------
# build_brief
# ---------------------------------------------------------------------------


def build_brief(
    spec: FsmSpec,
    state: State,
    env: dict[str, Any],
    run_id: uuid.UUID,
    iteration_n: int | None = None,
    brief_id: uuid.UUID | None = None,
) -> Brief:
    """Materialise a :class:`Brief` for ``state`` in the context of ``env``.

    Parameters
    ----------
    spec:
        The owning :class:`FsmSpec`. Only its ``id`` is used here but
        passing the whole spec keeps the call sites unified with the
        higher-level driver.
    state:
        The state being entered (or whose loop is about to iterate
        again).
    env:
        The current run environment. Worker ``inputs`` are pulled from
        this mapping by name; missing keys map to ``None``.
    run_id:
        The id of the FSM run this brief belongs to.
    iteration_n:
        For loop states, the 1-based iteration index this brief
        represents. Ignored for non-loop states. Defaults to ``1`` when
        the state is a loop and no value is supplied.
    brief_id:
        Optional pre-minted brief id. When ``None`` a fresh UUIDv7 is
        generated (with a UUIDv4 fallback if ``uuid-utils`` is absent).

    Returns
    -------
    Brief
        A fully-populated :class:`Brief` with both the spec-level
        metadata (purpose, preconditions, transitions, outputs
        expected) and the runtime extras (resolved inputs, loop
        iteration index, outputs path) needed by the worker layer.
    """
    is_loop = state.loop is not None

    worker: Worker | None
    inputs: dict[str, Any]
    outputs_path: str | None = None
    effective_iteration: int | None = None
    loop_for_brief: Loop | None = state.loop

    if is_loop:
        loop: Loop = state.loop  # type: ignore[assignment]
        worker = loop.worker
        effective_iteration = iteration_n if iteration_n is not None else 1
        outputs_path = outputs_path_for(state, effective_iteration)
        inputs = {name: env.get(name) for name in worker.inputs}
        has_worker = True
    else:
        worker = state.worker
        if worker is not None:
            inputs = {name: env.get(name) for name in worker.inputs}
            has_worker = True
        else:
            inputs = {}
            has_worker = False

    # W23f: render the worker's prompt template through the sandboxed
    # PromptRenderer when it carries Jinja constructs. Plain prompts
    # pass through verbatim with zero overhead. For loop states the
    # rendered worker is mirrored back onto a Loop.model_copy clone so
    # the brief's `loop` field exposes the same rendered prompt as its
    # top-level `worker` field, so consumers reading either surface see a
    # consistent rendered template.
    if worker is not None:
        rendered_worker = _maybe_render_prompt(
            spec, state, worker, env, effective_iteration
        )
        worker = rendered_worker
        if is_loop and loop_for_brief is not None:
            loop_for_brief = loop_for_brief.model_copy(
                update={"worker": rendered_worker}
            )

    return Brief(
        run_id=run_id,
        fsm_id=spec.id,
        state=state.id,
        purpose=state.purpose,
        preconditions=list(state.preconditions),
        inputs=inputs,
        outputs_expected=list(state.outputs),
        post_validations=list(state.post_validations),
        transitions=list(state.transitions),
        has_worker=has_worker,
        has_loop=is_loop,
        allowed_tools=list(state.allowed_tools),
        worker=worker,
        loop=loop_for_brief,
        # W23g: surface the gate body when this state is a gate state.
        # The engine pause-on-gate dispatch + fsm.resolve_gate MCP tool
        # consume this in a follow-up commit; the brief surface is
        # locked here so MCP / API / UI clients can recognise gate
        # briefs immediately.
        gate=state.gate,
        iteration_n=effective_iteration,
        outputs_path=outputs_path,
        brief_id=brief_id if brief_id is not None else _new_brief_id(),
    )


# ---------------------------------------------------------------------------
# validate_output
# ---------------------------------------------------------------------------


def _schema_for_state(state: State) -> ResponseSchema | None:
    """Return the :class:`ResponseSchema` associated with ``state``, if any.

    For loop states the schema lives on ``state.loop.worker``; for
    plain worker states it lives on ``state.worker``. States without
    a worker (and without a loop) have no schema and the function
    returns ``None``.
    """
    if state.loop is not None:
        return state.loop.worker.response_schema
    if state.worker is not None:
        return state.worker.response_schema
    return None


def validate_output(state: State, outputs: dict[str, Any]) -> ValidationResult:
    """Validate ``outputs`` against the state's worker response schema.

    A state with no schema (no worker / no loop, or a worker that
    declared no ``response_schema``) is treated as always valid — the
    engine has no opinion about output shape in that case.

    Returns
    -------
    ValidationResult
        ``valid=True`` and an empty error list on success; otherwise
        ``valid=False`` with a list of human-readable JSON-schema
        violation messages.
    """
    schema = _schema_for_state(state)
    if schema is None:
        return ValidationResult(valid=True, errors=[])

    valid, errors = schema.model_validate_json_payload(outputs)
    return ValidationResult(valid=valid, errors=errors)


# ---------------------------------------------------------------------------
# resolve_transition
# ---------------------------------------------------------------------------


def _evaluate_guard(
    transition: Transition,
    env: dict[str, Any],
    any_earlier_matched: bool,
    judgement_pick: str | None,
) -> TransitionEvaluation:
    """Evaluate a single transition guard and package the result.

    The four guard shapes are dispatched here:

    * ``"always"`` literal or ``{"kind": "always"}`` dict → unconditional True.
    * ``"otherwise"`` literal → True iff no earlier transition matched.
    * :class:`Predicate` (or ``{"kind": "deterministic", ...}`` dict that
      was normalised into one upstream) → run through
      :func:`evaluate_expression`.
    * ``{"kind": "judgement", ...}`` dict → True iff
      ``judgement_pick == transition.to``.

    Any error from the predicate evaluator is captured on the
    :class:`TransitionEvaluation`'s ``error`` field with ``result=False``
    rather than propagating.
    """
    when = transition.when

    # ``TransitionKind`` members — the always/otherwise literals.
    # Checked BEFORE the bare-string branch because StrEnum members
    # are also ``str`` subclasses, but we want the enum equality.
    if isinstance(when, TransitionKind):
        if when is TransitionKind.always:
            return TransitionEvaluation(
                to=transition.to,
                when=when,
                result=True,
                kind=TransitionKind.always.value,
            )
        if when is TransitionKind.otherwise:
            return TransitionEvaluation(
                to=transition.to,
                when=when,
                result=not any_earlier_matched,
                kind=TransitionKind.otherwise.value,
            )
        # Defensive: TransitionKind.deterministic and .judgement are
        # carried as Predicate / dict shapes, not as raw enum members on
        # ``Transition.when``. A bare enum member here for one of those
        # kinds is a programming bug — surface it loudly.
        return TransitionEvaluation(
            to=transition.to,
            when=when,
            result=False,
            kind="unknown",
            error=f"transition `when` cannot be bare {when!r}",
        )

    # Predicate guard (deterministic).
    if isinstance(when, Predicate):
        try:
            result = evaluate_expression(when.expression, env)
        except (PredicateParseError, PredicateEvalError) as exc:
            return TransitionEvaluation(
                to=transition.to,
                when={"expression": when.expression},
                result=False,
                expression=when.expression,
                kind=TransitionKind.deterministic.value,
                error=str(exc),
            )
        return TransitionEvaluation(
            to=transition.to,
            when={"expression": when.expression},
            result=bool(result),
            expression=when.expression,
            kind=TransitionKind.deterministic.value,
        )

    # Dict guards: kind == always / otherwise / deterministic / judgement.
    if isinstance(when, dict):
        kind_raw = when.get("kind")
        if kind_raw == TransitionKind.always.value:
            return TransitionEvaluation(
                to=transition.to,
                when=when,
                result=True,
                kind=TransitionKind.always.value,
            )
        if kind_raw == TransitionKind.otherwise.value:
            return TransitionEvaluation(
                to=transition.to,
                when=when,
                result=not any_earlier_matched,
                kind=TransitionKind.otherwise.value,
            )
        if kind_raw == TransitionKind.deterministic.value:
            expression = when.get("expression")
            if not isinstance(expression, str):
                return TransitionEvaluation(
                    to=transition.to,
                    when=when,
                    result=False,
                    kind=TransitionKind.deterministic.value,
                    error="deterministic guard missing string `expression`",
                )
            try:
                result = evaluate_expression(expression, env)
            except (PredicateParseError, PredicateEvalError) as exc:
                return TransitionEvaluation(
                    to=transition.to,
                    when=when,
                    result=False,
                    expression=expression,
                    kind=TransitionKind.deterministic.value,
                    error=str(exc),
                )
            return TransitionEvaluation(
                to=transition.to,
                when=when,
                result=bool(result),
                expression=expression,
                kind=TransitionKind.deterministic.value,
            )
        if kind_raw == TransitionKind.judgement.value:
            criteria = when.get("criteria")
            return TransitionEvaluation(
                to=transition.to,
                when=when,
                result=judgement_pick == transition.to,
                kind=TransitionKind.judgement.value,
                criteria=criteria if isinstance(criteria, str) else None,
            )

        return TransitionEvaluation(
            to=transition.to,
            when=when,
            result=False,
            kind="unknown",
            error=f"unrecognised guard dict shape: {sorted(when.keys())!r}",
        )

    # Anything else is a programming error — surface it explicitly.
    return TransitionEvaluation(
        to=transition.to,
        when=str(when),
        result=False,
        kind="unknown",
        error=f"unrecognised guard type: {type(when).__name__}",
    )


def resolve_transition(
    state: State,
    env: dict[str, Any],
    judgement_pick: str | None = None,
) -> tuple[Transition | None, list[TransitionEvaluation]]:
    """Find the first matching transition out of ``state``.

    Iterates the transitions in declared order. Each guard is
    evaluated via :func:`_evaluate_guard`; the first one whose
    evaluation returns ``result=True`` wins. The function always
    returns the full list of :class:`TransitionEvaluation` records (one
    per transition examined, including any transitions skipped after
    the winner — they are still evaluated and reported for forensic
    completeness).

    Returns
    -------
    (transition, evaluations)
        ``transition`` is the winning :class:`Transition` or ``None``
        when no guard matched. ``evaluations`` is the trace.
    """
    evaluations: list[TransitionEvaluation] = []
    winner: Transition | None = None
    any_matched = False

    for transition in state.transitions:
        evaluation = _evaluate_guard(
            transition,
            env,
            any_earlier_matched=any_matched,
            judgement_pick=judgement_pick,
        )
        evaluations.append(evaluation)
        if evaluation.result and winner is None:
            winner = transition
            any_matched = True

    return winner, evaluations


# ---------------------------------------------------------------------------
# run_post_validations
# ---------------------------------------------------------------------------


def run_post_validations(
    state: State,
    outputs: dict[str, Any],
) -> PostValidationResult:
    """Run every post-validation predicate declared on ``state``.

    Each predicate is evaluated against ``outputs`` as the environment
    (post-validations are intended to inspect the worker's outputs in
    isolation, before they are merged into the wider run env). A parse
    or evaluation error is captured on the
    :class:`PostValidationResultEntry`'s ``error`` field with
    ``result=False`` — it never raises.

    The aggregate ``valid`` flag is the logical AND of every individual
    ``result``. A state with no post-validations is trivially valid.
    """
    entries: list[PostValidationResultEntry] = []
    all_valid = True

    for predicate in state.post_validations:
        # Pre-validate the expression so we surface a parse error as a
        # clean ``error`` field instead of letting it bubble up.
        if not validate_expression(predicate.expression):
            entries.append(
                PostValidationResultEntry(
                    check="post_validation",
                    expression=predicate.expression,
                    result=False,
                    error="malformed predicate expression",
                )
            )
            all_valid = False
            continue
        try:
            result = evaluate_expression(predicate.expression, outputs)
        except (PredicateParseError, PredicateEvalError) as exc:
            entries.append(
                PostValidationResultEntry(
                    check="post_validation",
                    expression=predicate.expression,
                    result=False,
                    error=str(exc),
                )
            )
            all_valid = False
            continue
        entries.append(
            PostValidationResultEntry(
                check="post_validation",
                expression=predicate.expression,
                result=bool(result),
            )
        )
        if not result:
            all_valid = False

    return PostValidationResult(valid=all_valid, results=entries)


# ---------------------------------------------------------------------------
# advance
# ---------------------------------------------------------------------------


def _normalise_outputs(outputs: dict[str, Any] | WorkerOutput) -> dict[str, Any]:
    """Accept either a raw dict or a :class:`WorkerOutput` envelope.

    Returns the inner ``outputs`` mapping. Other types raise a
    ``TypeError`` so a mis-typed call site is caught immediately rather
    than silently producing a wrong-shaped result.
    """
    if isinstance(outputs, WorkerOutput):
        return dict(outputs.outputs)
    if isinstance(outputs, dict):
        return outputs
    raise TypeError(
        f"advance() expected dict or WorkerOutput, got {type(outputs).__name__}"
    )


def advance(
    spec: FsmSpec,
    run_ctx: RunCtx,
    outputs: dict[str, Any] | WorkerOutput,
    judgement_pick: str | None = None,
) -> EngineAdvanceResult:
    """Drive one engine step from ``run_ctx.current_state`` given ``outputs``.

    The order of operations is intentionally fixed so the resulting
    :class:`EngineAdvanceResult` is deterministic with respect to the
    inputs:

    1. Look up the current state from ``spec``.
    2. Validate the outputs against the state's response schema
       (faults with ``reason="output_schema_violation"`` on failure).
    3. If the state is a loop, ask the loop module whether to continue.
       When the loop wants another iteration we short-circuit with a
       ``loop_continue`` result that already contains the next
       iteration's brief — the post-validations and transitions are
       only run on the iteration that terminates the loop.
    4. Run post-validations on the outputs (faults with
       ``reason="post_validation_failed"`` on failure).
    5. Merge outputs into ``run_ctx.env`` to form the transition
       environment.
    6. Resolve the outgoing transition against that merged env.
    7. If no transitions are declared on the state, the state is
       terminal: emit a ``terminal`` result carrying ``verdict``.
       If transitions exist but none matched, fault with
       ``reason="no_transition_matched"``.
    8. Otherwise emit an ``advance`` result with the next brief
       pre-materialised so the caller can hand it straight to the next
       worker without a second round-trip.
    """
    state = spec.get_state(run_ctx.current_state)
    raw_outputs = _normalise_outputs(outputs)

    # Step 2: response-schema validation.
    validation = validate_output(state, raw_outputs)
    if not validation.valid:
        return EngineAdvanceResult(
            kind=EngineAdvanceKind.fault,
            reason="output_schema_violation",
            errors=list(validation.errors),
        )

    # Step 3: loop continuation decision.
    if state.loop is not None:
        current_iter = run_ctx.iteration_n if run_ctx.iteration_n is not None else 1
        decision: LoopDecision = loop_decide(state, raw_outputs, current_iter)
        if not decision.terminate:
            next_iter = current_iter + 1
            next_brief = build_brief(
                spec,
                state,
                run_ctx.env,
                run_id=run_ctx.run_id,
                iteration_n=next_iter,
            )
            return EngineAdvanceResult(
                kind=EngineAdvanceKind.loop_continue,
                brief=next_brief,
                iteration_n=next_iter,
            )

    # Step 4: post-validations.
    post = run_post_validations(state, raw_outputs)
    if not post.valid:
        return EngineAdvanceResult(
            kind=EngineAdvanceKind.fault,
            reason="post_validation_failed",
            post_validations=list(post.results),
        )

    # Step 5: merge outputs into env for transition evaluation.
    env_with_outputs: dict[str, Any] = {**run_ctx.env, **raw_outputs}

    # Step 6: resolve the outgoing transition.
    transition, evaluations = resolve_transition(state, env_with_outputs, judgement_pick)

    # Step 7a: terminal state (no transitions at all).
    if transition is None and not state.transitions:
        return EngineAdvanceResult(
            kind=EngineAdvanceKind.terminal,
            verdict=env_with_outputs.get("verdict"),
            evaluations=evaluations,
        )

    # Step 7b: declared transitions but none matched.
    if transition is None:
        return EngineAdvanceResult(
            kind=EngineAdvanceKind.fault,
            reason="no_transition_matched",
            evaluations=evaluations,
        )

    # Step 8: an outgoing transition matched — build the next brief.
    next_state = spec.get_state(transition.to)
    next_brief = build_brief(
        spec,
        next_state,
        env_with_outputs,
        run_id=run_ctx.run_id,
    )

    # W23g: the incoming state is a gate. The engine MUST NOT invoke a
    # worker on a gate state; the brief carries the Gate body so the
    # orchestrator surface can recognise the gate and route the next
    # call to fsm.resolve_gate (LLM-supplied value or run_output
    # binding) instead of fsm.commit_outputs. The brief is still
    # populated so the caller has the gate's response_schema +
    # source_kind + bindings in hand without a second round-trip.
    if next_state.kind is StateKind.gate:
        return EngineAdvanceResult(
            kind=EngineAdvanceKind.gate_pending,
            next_state=next_state.id,
            brief=next_brief,
            evaluations=evaluations,
        )

    return EngineAdvanceResult(
        kind=EngineAdvanceKind.advance,
        next_state=next_state.id,
        brief=next_brief,
        evaluations=evaluations,
    )


# ---------------------------------------------------------------------------
# execute_inline — server-side deterministic state handler
# ---------------------------------------------------------------------------


def execute_inline(
    state: State,
    ctx: RunCtx,
    args: dict[str, Any],
    inputs: dict[str, Any],
    registry: InlineHandlerRegistry | None = None,
) -> InlineExecutionResult:
    """Run an inline state's registered handler and report the outcome.

    This function is the pure entry point the W2 SQLite repository
    layer will call inside its ``@atomic`` transaction to advance
    through inline states without an LLM round-trip. It is intentionally
    side-effect-free: it does NOT mutate any persistence, does NOT
    emit events, and does NOT call back into :func:`advance`. The
    caller wraps the result in a transaction, persists the inline
    state's outputs + emits ``inline_executed`` / ``inline_failed``
    events, then calls :func:`advance` with the handler's outputs to
    resolve the outgoing transition.

    Parameters
    ----------
    state:
        The inline state being executed. Must have ``state.inline``
        non-None; otherwise the function raises ``TypeError`` because
        the call site is a programming error.
    ctx:
        The current :class:`RunCtx`. ``ctx.iteration_n`` is passed
        through to the handler via :class:`InlineContext` so handlers
        embedded in loop bodies (a future capability) can see their
        iteration index. ``ctx.env`` is not threaded into the inline
        context — the handler's input surface is ``args`` + ``inputs``,
        mirroring the worker brief's input shape.
    args:
        The owning run's startup ``args`` dict. Passed through verbatim
        on the :class:`InlineContext`.
    inputs:
        The inputs dict resolved by the caller from prior-state outputs,
        in the same shape :class:`~ctxr.fsm.core.models.Brief.inputs`
        carries.
    registry:
        Optional :class:`InlineHandlerRegistry` to look up the handler
        in. When ``None``, the module-level default returned by
        :func:`get_default_registry` is used.

    Returns
    -------
    InlineExecutionResult
        ``ok=True`` and ``outputs`` populated on success. ``ok=False``
        with a typed :class:`InlineFaultReason` on every failure mode:

        * :attr:`InlineFaultReason.unregistered` — no handler registered
          for ``(ctx.fsm_id, state.inline.handler_id)``;
          :attr:`InlineExecutionResult.fault_detail` carries the
          missing key as ``"<fsm_id>/<handler_id>"``.
        * :attr:`InlineFaultReason.raised` — handler raised an
          exception; ``fault_detail`` carries the exception type name +
          ``str()`` (full traceback is NOT captured here; SQLite-layer
          callers log it from their own try/except boundary).
        * :attr:`InlineFaultReason.bad_return_type` — handler returned
          something other than ``dict[str, Any]``; ``fault_detail``
          names the offending type.
        * :attr:`InlineFaultReason.validation_failed` — handler returned
          a dict but it did not validate against
          ``state.inline.response_schema``; ``validation.errors``
          carries the per-field messages.
        * :attr:`InlineFaultReason.post_validation_failed` — handler
          returned a schema-valid dict but at least one
          :attr:`~ctxr.fsm.core.models.InlineSpec.post_validations`
          predicate evaluated to ``False``; ``post_validations``
          carries the per-predicate trace.
    """
    if state.inline is None:
        raise TypeError(
            f"execute_inline called on state {state.id!r} which is not an inline state"
        )

    inline_spec = state.inline
    handler_id = inline_spec.handler_id
    chosen_registry = registry if registry is not None else get_default_registry()

    handler = chosen_registry.lookup(ctx.fsm_id, handler_id)
    if handler is None:
        return InlineExecutionResult(
            handler_id=handler_id,
            ok=False,
            outputs={},
            validation=ValidationResult(valid=True, errors=[]),
            post_validations=None,
            fault_reason=InlineFaultReason.unregistered,
            fault_detail=f"{ctx.fsm_id}/{handler_id}",
        )

    # Build the handler's read-only context envelope.
    inline_ctx = InlineContext(
        run_id=ctx.run_id,
        fsm_id=ctx.fsm_id,
        state_id=state.id,
        iteration_n=ctx.iteration_n,
        args=dict(args),
        inputs=dict(inputs),
    )

    # Invoke the handler; capture any raise as a structured fault.
    try:
        raw_output = handler(inline_ctx)
    except Exception as exc:
        return InlineExecutionResult(
            handler_id=handler_id,
            ok=False,
            outputs={},
            validation=ValidationResult(valid=True, errors=[]),
            post_validations=None,
            fault_reason=InlineFaultReason.raised,
            fault_detail=f"{type(exc).__name__}: {exc}",
        )

    # The handler MUST return a dict; anything else is a contract bug.
    if not isinstance(raw_output, dict):
        return InlineExecutionResult(
            handler_id=handler_id,
            ok=False,
            outputs={},
            validation=ValidationResult(valid=True, errors=[]),
            post_validations=None,
            fault_reason=InlineFaultReason.bad_return_type,
            fault_detail=f"expected dict, got {type(raw_output).__name__}",
        )

    # Validate against the optional response schema. A schema-less
    # inline state is treated as always-valid (same convention the
    # worker path uses in ``validate_output``).
    if inline_spec.response_schema is None:
        validation = ValidationResult(valid=True, errors=[])
    else:
        valid, errors = inline_spec.response_schema.model_validate_json_payload(
            raw_output
        )
        validation = ValidationResult(valid=valid, errors=errors)
        if not valid:
            return InlineExecutionResult(
                handler_id=handler_id,
                ok=False,
                outputs={},
                validation=validation,
                post_validations=None,
                fault_reason=InlineFaultReason.validation_failed,
                fault_detail=None,
            )

    # Run the inline-state's post-validation predicates (if any). The
    # shape mirrors ``run_post_validations`` on workers but evaluates
    # against ``raw_output`` directly — same convention.
    post_result: PostValidationResult | None = None
    if inline_spec.post_validations:
        entries: list[PostValidationResultEntry] = []
        all_valid = True
        for predicate in inline_spec.post_validations:
            if not validate_expression(predicate.expression):
                entries.append(
                    PostValidationResultEntry(
                        check="inline_post_validation",
                        expression=predicate.expression,
                        result=False,
                        error="malformed predicate expression",
                    )
                )
                all_valid = False
                continue
            try:
                result = evaluate_expression(predicate.expression, raw_output)
            except (PredicateParseError, PredicateEvalError) as exc:
                entries.append(
                    PostValidationResultEntry(
                        check="inline_post_validation",
                        expression=predicate.expression,
                        result=False,
                        error=str(exc),
                    )
                )
                all_valid = False
                continue
            entries.append(
                PostValidationResultEntry(
                    check="inline_post_validation",
                    expression=predicate.expression,
                    result=bool(result),
                )
            )
            if not result:
                all_valid = False
        post_result = PostValidationResult(valid=all_valid, results=entries)
        if not all_valid:
            return InlineExecutionResult(
                handler_id=handler_id,
                ok=False,
                outputs={},
                validation=validation,
                post_validations=post_result,
                fault_reason=InlineFaultReason.post_validation_failed,
                fault_detail=None,
            )

    # All-clear: handler returned a dict, the schema accepts it, and
    # every post-validation passed.
    return InlineExecutionResult(
        handler_id=handler_id,
        ok=True,
        outputs=dict(raw_output),
        validation=validation,
        post_validations=post_result,
        fault_reason=None,
        fault_detail=None,
    )


# ---------------------------------------------------------------------------
# resolve_gate — W23g cross-FSM gate resolution
# ---------------------------------------------------------------------------


class GateResolutionError(ValueError):
    """Typed error raised by :func:`resolve_gate` for the GATE_CONTRACT envelopes.

    The ``code`` attribute is one of the snake_case envelope vocabulary
    keys documented in ``ctxr/fsm/memory/GATE_CONTRACT.md``:

    * ``gate_schema_mismatch`` — the resolved value did not validate
      against ``Gate.response_schema``.
    * ``gate_value_or_binding_required`` — neither ``value`` nor a
      binding was supplied.
    * ``gate_value_and_binding_conflict`` — both were supplied; the
      contract requires exactly one.
    * ``gate_source_kind_mismatch`` — the supplied resolution shape
      did not match the gate's ``source_kind`` (``binding`` on an
      ``llm_supplied`` gate, or ``value`` on a ``run_output`` gate).

    The MCP tool layer (:func:`ctxr.fsm.mcp.tools_runs.fsm_resolve_gate`)
    catches this and maps the ``code`` straight onto the wire envelope
    so clients can branch on the same vocabulary they read in
    GATE_CONTRACT.md.
    """

    def __init__(self, code: str, detail: str, *, errors: list[str] | None = None):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.errors: list[str] = list(errors or [])


class GateResolution(BaseModel):
    """The outcome of :func:`resolve_gate`.

    * :attr:`env_update` — the dict of ``{target_field: resolved_value}``
      entries the caller should merge into the run's env. The current
      :func:`resolve_gate` API accepts a single optional
      :class:`GateBinding` and a single optional ``value``, so
      ``env_update`` always carries exactly one entry: for
      ``llm_supplied`` gates it is keyed off the gate state's first
      declared output; for ``run_output`` gates it is keyed off the
      supplied binding's ``target_field``.
    * :attr:`transitions` — the list of :class:`Transition` declared on
      the gate state, surfaced for the caller so the persistence layer
      can drive the outgoing transition without re-walking the spec.

    The MCP tool body merges ``env_update`` into the run env, records
    a ``gate_resolved`` event, persists the binding row, then advances
    the engine through the gate's outgoing transitions.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    env_update: dict[str, Any] = Field(default_factory=dict)
    transitions: list[Transition] = Field(default_factory=list)


def _validate_against_gate_schema(
    gate: Gate,
    payload: dict[str, Any],
) -> None:
    """Validate ``payload`` against the gate's ``response_schema``.

    Raises :class:`GateResolutionError` with ``code='gate_schema_mismatch'``
    when validation fails. Uses the same
    :meth:`ResponseSchema.model_validate_json_payload` helper the
    worker-output path uses so the error vocabulary stays uniform across
    the engine.
    """
    valid, errors = gate.response_schema.model_validate_json_payload(payload)
    if not valid:
        raise GateResolutionError(
            "gate_schema_mismatch",
            "resolved gate value did not match Gate.response_schema",
            errors=errors,
        )


def resolve_gate(
    spec: FsmSpec,
    state: State,
    env: dict[str, Any],
    run_id: uuid.UUID,
    *,
    value: dict[str, Any] | None = None,
    binding: GateBinding | None = None,
    binding_value: Any = None,
) -> GateResolution:
    """Resolve a gate state's pending value and return the env delta + transitions.

    Parameters
    ----------
    spec, state:
        The owning :class:`FsmSpec` and the gate :class:`State`. ``state``
        MUST have ``state.gate`` non-None — otherwise a ``TypeError``
        surfaces immediately because the caller is feeding a non-gate
        state into the resolver.
    env:
        The run env at gate-entry time. Reserved for future use (a
        gate's ``response_schema`` could in principle reference
        ``env``-shaped properties); currently the env is read-only.
    run_id:
        The run id this resolution belongs to. Currently unused in the
        pure-engine resolver but threaded through so future telemetry
        hooks (the event-bus payloads minted by the MCP layer) carry
        consistent identifiers.
    value:
        LLM-supplied literal. Required when ``gate.source_kind`` is
        ``llm_supplied``; validated against the gate's
        ``response_schema`` and landed under the gate state's first
        declared :attr:`State.outputs` entry.
    binding:
        :class:`GateBinding` describing where the resolved value came
        from. Required when ``gate.source_kind`` is ``run_output``;
        ``binding.target_field`` names the env key the resolved value
        lands under.
    binding_value:
        The actual value pulled from the source run's state output
        (only used when ``binding`` is supplied). The MCP layer reads
        the source via :meth:`Project.runs` (or equivalent) and threads
        the looked-up value here; the pure engine resolver itself has
        no I/O so it cannot perform the lookup.

    Returns
    -------
    GateResolution
        A typed envelope carrying:

        * ``env_update`` — the dict of resolved fields to merge into
          the run env.
        * ``transitions`` — the gate state's outgoing transitions,
          ready for the persistence layer to walk and drive the next
          state entry.

    Raises
    ------
    GateResolutionError
        On any of the GATE_CONTRACT-vocabulary envelope codes:

        * ``gate_value_or_binding_required`` — neither ``value`` nor
          ``binding`` was supplied.
        * ``gate_value_and_binding_conflict`` — both ``value`` and
          ``binding`` were supplied; the contract requires exactly
          one.
        * ``gate_source_kind_mismatch`` — the supplied resolution
          shape does not match the gate's ``source_kind`` (a
          ``run_output`` gate was handed a literal ``value``, or an
          ``llm_supplied`` gate was handed a ``binding``).
        * ``gate_schema_mismatch`` — the resolved value did not pass
          the gate's response schema.
    """
    if state.gate is None:
        raise TypeError(
            f"resolve_gate called on state {state.id!r} which is not a gate state"
        )

    gate: Gate = state.gate

    if value is None and binding is None:
        raise GateResolutionError(
            "gate_value_or_binding_required",
            "fsm.resolve_gate requires exactly one of `value` or `binding`",
        )
    if value is not None and binding is not None:
        raise GateResolutionError(
            "gate_value_and_binding_conflict",
            "fsm.resolve_gate requires exactly one of `value` or `binding`, not both",
        )

    # source_kind enforcement: the gate's declared source_kind dictates
    # which resolution shape is legal. A run_output gate resolved via a
    # literal `value` would bypass the binding-lookup + max_age_ms
    # staleness semantics; an llm_supplied gate resolved via a
    # `binding` would persist an unintended cross-run dependency (and
    # advertise it in the gate_bindings topology index). Reject both
    # before we touch the schema validator or persistence layer.
    if binding is not None and gate.source_kind is not GateSourceKind.run_output:
        raise GateResolutionError(
            "gate_source_kind_mismatch",
            (
                f"gate {state.id!r} has source_kind={gate.source_kind.value!r}; "
                "a `binding` is only valid for source_kind='run_output'"
            ),
        )
    if value is not None and gate.source_kind is not GateSourceKind.llm_supplied:
        raise GateResolutionError(
            "gate_source_kind_mismatch",
            (
                f"gate {state.id!r} has source_kind={gate.source_kind.value!r}; "
                "a literal `value` is only valid for source_kind='llm_supplied'"
            ),
        )

    env_update: dict[str, Any] = {}

    if binding is not None:
        # run_output path. The binding's target_field names the env
        # key the resolved value lands under. We validate the supplied
        # binding_value against the gate's response_schema so a
        # spec-evolved schema cannot silently land an out-of-shape
        # value in the downstream run's env.
        payload = {binding.target_field: binding_value}
        _validate_against_gate_schema(gate, payload)
        env_update[binding.target_field] = binding_value
    else:
        # llm_supplied path. The LLM passes a dict that must validate
        # against the gate's response_schema. The resolved fields land
        # under the gate state's first declared output (GATE_CONTRACT
        # rule: "lands in the run's environment under the gate's
        # `target_field` (defaults to the gate state's first declared
        # output)"). We use the whole validated value under that key,
        # which matches what a worker's outputs would look like.
        assert value is not None  # narrow for the type checker
        _validate_against_gate_schema(gate, value)
        if not state.outputs:
            raise GateResolutionError(
                "gate_schema_mismatch",
                f"gate state {state.id!r} has no declared outputs to land the value under",
            )
        target_field = state.outputs[0]
        if target_field in value:
            # Common case: the gate's response_schema is shaped as
            # {target_field: <typed value>}; we lift the inner value
            # so downstream env reads see the same shape they would
            # see for a worker that returned {target_field: ...}.
            env_update[target_field] = value[target_field]
        else:
            env_update[target_field] = value

    return GateResolution(
        env_update=env_update,
        transitions=list(state.transitions),
    )
