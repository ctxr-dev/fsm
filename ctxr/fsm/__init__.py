"""Top-level ergonomic facade for ``ctxr.fsm``.

This module re-exports the full public API of :mod:`ctxr.fsm.core` so
that the ergonomic call site::

    from ctxr.fsm import FsmSpec, State, Transition, Worker, Loop, Predicate

works without dragging callers through the ``ctxr.fsm.core`` subpackage.
Every symbol listed in :data:`ctxr.fsm.core.__all__` is re-exported
verbatim here, so any name resolvable via ``ctxr.fsm.core`` is also
resolvable via ``ctxr.fsm`` with identical semantics.

The package itself is a PEP 420 namespace package at the ``ctxr`` level
(no ``ctxr/__init__.py``); only ``ctxr.fsm`` carries an init module.
This lets sibling distributions (``ctxr-fsm-mcp``, ``ctxr-fsm-api``, …)
later live under the same ``ctxr.*`` namespace without packaging
conflicts.

The :data:`__version__` constant tracks the published package version
declared in ``pyproject.toml``.
"""

from __future__ import annotations

from ctxr.fsm.core import (
    # ── aggregator ────────────────────────────────────────────────────
    AggregatedAcrossStates,
    AggregatedLoop,
    # ── engine value objects ──────────────────────────────────────────
    AllowedTools,
    Brief,
    CommitSignature,
    CommitToken,
    # ── enums ──────────────────────────────────────────────────────────
    DeliveryStatus,
    # ── engine ────────────────────────────────────────────────────────
    EngineAdvanceResult,
    # ── protocols ─────────────────────────────────────────────────────
    EventBus,
    EventKind,
    # ── spec primitives ───────────────────────────────────────────────
    FsmSpec,
    # ── spec validation & hashing ─────────────────────────────────────
    FsmValidationResult,
    JournalProtocol,
    Lock,
    Loop,
    # ── loop ──────────────────────────────────────────────────────────
    LoopConfigError,
    LoopDecision,
    PostValidationResult,
    PostValidationResultEntry,
    Predicate,
    # ── predicates ────────────────────────────────────────────────────
    PredicateEvalError,
    PredicateParseError,
    Repository,
    ResponseSchema,
    RunCtx,
    RunStatus,
    SignalKind,
    State,
    StateStatus,
    Transition,
    TransitionEvaluation,
    TransitionKind,
    ValidationResult,
    VerifierHandler,
    VerifierOutcome,
    VerifierSpec,
    VerifierVerdict,
    VerifierVote,
    Worker,
    WorkerOutput,
    advance,
    aggregate_across_states,
    aggregate_loop_outputs,
    attach_methods,
    build_brief,
    evaluate_expression,
    fsm_spec_hash,
    get_verifier_handler,
    loop_decide,
    outputs_path_for,
    resolve_transition,
    run_post_validations,
    run_verifier,
    set_verifier_handler,
    validate_expression,
    validate_fsm_spec,
    validate_output,
)

__version__ = "0.1.0a1"


__all__ = [
    "AggregatedAcrossStates",
    # ── aggregator ────────────────────────────────────────────────────
    "AggregatedLoop",
    "AllowedTools",
    # ── engine value objects ──────────────────────────────────────────
    "Brief",
    "CommitSignature",
    "CommitToken",
    "DeliveryStatus",
    # ── engine ────────────────────────────────────────────────────────
    "EngineAdvanceResult",
    "EventBus",
    "EventKind",
    "FsmSpec",
    # ── spec validation & hashing ─────────────────────────────────────
    "FsmValidationResult",
    "JournalProtocol",
    "Lock",
    "Loop",
    # ── loop ──────────────────────────────────────────────────────────
    "LoopConfigError",
    "LoopDecision",
    "PostValidationResult",
    "PostValidationResultEntry",
    "Predicate",
    "PredicateEvalError",
    # ── predicates ────────────────────────────────────────────────────
    "PredicateParseError",
    # ── protocols ─────────────────────────────────────────────────────
    "Repository",
    # ── spec primitives ───────────────────────────────────────────────
    "ResponseSchema",
    "RunCtx",
    # ── enums ──────────────────────────────────────────────────────────
    "RunStatus",
    "SignalKind",
    "State",
    "StateStatus",
    "Transition",
    "TransitionEvaluation",
    "TransitionKind",
    "ValidationResult",
    "VerifierHandler",
    "VerifierOutcome",
    "VerifierSpec",
    "VerifierVerdict",
    "VerifierVote",
    "Worker",
    "WorkerOutput",
    "__version__",
    "advance",
    "aggregate_across_states",
    "aggregate_loop_outputs",
    "attach_methods",
    "build_brief",
    "evaluate_expression",
    "fsm_spec_hash",
    "get_verifier_handler",
    "loop_decide",
    "outputs_path_for",
    "resolve_transition",
    "run_post_validations",
    "run_verifier",
    "set_verifier_handler",
    "validate_expression",
    "validate_fsm_spec",
    "validate_output",
]
