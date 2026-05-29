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
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ctxr.fsm.core.loop import decide as loop_decide
from ctxr.fsm.core.loop import outputs_path_for
from ctxr.fsm.core.models import (
    Brief,
    FsmSpec,
    Loop,
    LoopDecision,
    PostValidationResult,
    PostValidationResultEntry,
    Predicate,
    ResponseSchema,
    RunCtx,
    State,
    Transition,
    TransitionEvaluation,
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

__all__ = [
    "EngineAdvanceResult",
    "advance",
    "build_brief",
    "resolve_transition",
    "run_post_validations",
    "validate_output",
]


# ---------------------------------------------------------------------------
# UUIDv7 helper (graceful fallback)
# ---------------------------------------------------------------------------


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

    This is a discriminated-union-like envelope keyed off ``kind``:

    * ``"loop_continue"`` — the state is a loop and the most recent
      iteration did not satisfy the done-field; ``iteration_n`` and
      ``brief`` are populated with the next iteration's parameters.
    * ``"fault"`` — validation, post-validation, or transition
      resolution failed; ``reason`` plus one of ``errors``,
      ``post_validations``, ``evaluations`` carries the diagnostic.
    * ``"terminal"`` — the state has no outgoing transitions and the
      run is therefore complete; ``verdict`` carries the optional
      ``verdict`` field lifted from the merged env.
    * ``"advance"`` — a transition matched; ``next_state`` plus
      ``brief`` describe where the run goes next, and ``evaluations``
      records the full evaluation trace.

    The model is strict and extra-forbid so a mistaken kwarg surfaces
    immediately instead of being silently dropped.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    kind: Literal["loop_continue", "fault", "terminal", "advance"]
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
        loop=state.loop,
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

    # "always" or "otherwise" string literals.
    if isinstance(when, str):
        if when == "always":
            return TransitionEvaluation(
                to=transition.to,
                when=when,
                result=True,
                kind="always",
            )
        if when == "otherwise":
            return TransitionEvaluation(
                to=transition.to,
                when=when,
                result=not any_earlier_matched,
                kind="otherwise",
            )
        # Defensive: pydantic only accepts the two literals above.
        return TransitionEvaluation(
            to=transition.to,
            when=when,
            result=False,
            kind="unknown",
            error=f"unrecognised string guard {when!r}",
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
                kind="deterministic",
                error=str(exc),
            )
        return TransitionEvaluation(
            to=transition.to,
            when={"expression": when.expression},
            result=bool(result),
            expression=when.expression,
            kind="deterministic",
        )

    # Dict guards: kind == "always" / "otherwise" / "deterministic" / "judgement".
    if isinstance(when, dict):
        kind = when.get("kind")
        if kind == "always":
            return TransitionEvaluation(
                to=transition.to,
                when=when,
                result=True,
                kind="always",
            )
        if kind == "otherwise":
            return TransitionEvaluation(
                to=transition.to,
                when=when,
                result=not any_earlier_matched,
                kind="otherwise",
            )
        if kind == "deterministic":
            expression = when.get("expression")
            if not isinstance(expression, str):
                return TransitionEvaluation(
                    to=transition.to,
                    when=when,
                    result=False,
                    kind="deterministic",
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
                    kind="deterministic",
                    error=str(exc),
                )
            return TransitionEvaluation(
                to=transition.to,
                when=when,
                result=bool(result),
                expression=expression,
                kind="deterministic",
            )
        if kind == "judgement":
            criteria = when.get("criteria")
            return TransitionEvaluation(
                to=transition.to,
                when=when,
                result=judgement_pick == transition.to,
                kind="judgement",
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
            kind="fault",
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
                kind="loop_continue",
                brief=next_brief,
                iteration_n=next_iter,
            )

    # Step 4: post-validations.
    post = run_post_validations(state, raw_outputs)
    if not post.valid:
        return EngineAdvanceResult(
            kind="fault",
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
            kind="terminal",
            verdict=env_with_outputs.get("verdict"),
            evaluations=evaluations,
        )

    # Step 7b: declared transitions but none matched.
    if transition is None:
        return EngineAdvanceResult(
            kind="fault",
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
    return EngineAdvanceResult(
        kind="advance",
        next_state=next_state.id,
        brief=next_brief,
        evaluations=evaluations,
    )
