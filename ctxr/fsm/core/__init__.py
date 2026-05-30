"""Public API of the ctxr FSM core (pure, dependency-light substrate).

This package is the bedrock of the ctxr FSM. Every higher layer (SQLite
persistence in W2, loop runtime in W3+, MCP / HTTP surfaces above that,
end-user agent harnesses on top of those) imports from here and from
nowhere lower. The core deliberately has zero dependencies on
SQLAlchemy, FastAPI, MCP, or any I/O surface — it is safe to import in
any environment that has ``pydantic`` (and ``jsonschema`` for runtime
JSON-schema validation of worker outputs).

The module re-exports the canonical public surface of the seven
sibling modules:

* :mod:`ctxr.fsm.core.models` — Pydantic models, StrEnums, and the
  engine value objects (``Brief``, ``WorkerOutput``, ``CommitSignature``,
  ``CommitToken``, ``ValidationResult``, ``PostValidationResult``,
  ``TransitionEvaluation``, ``LoopDecision``, ``RunCtx``,
  ``AllowedTools``).
* :mod:`ctxr.fsm.core.engine` — pure FSM runtime: ``build_brief``,
  ``validate_output``, ``resolve_transition``, ``run_post_validations``,
  ``advance``, and the discriminated :class:`EngineAdvanceResult`.
* :mod:`ctxr.fsm.core.loop` — loop iteration mechanics: ``loop_decide``,
  ``outputs_path_for``, ``LoopConfigError``.
* :mod:`ctxr.fsm.core.aggregator` — pure aggregation helpers and their
  immutable result models.
* :mod:`ctxr.fsm.core.spec` — structural validation and canonical
  hashing of :class:`FsmSpec`. Importing this module attaches
  ``spec.validate()`` and ``spec.hash()`` instance methods so the
  ergonomic surface is always live.
* :mod:`ctxr.fsm.core.predicates` — sandboxed DSL evaluator used by
  transition guards and post-validations.
* :mod:`ctxr.fsm.core.protocols` — typing ``Protocol`` surface that the
  W2 SQLite layer will satisfy.

The :func:`ctxr.fsm.core.spec.attach_methods` shim is invoked at module
import time so ``FsmSpec(...).validate()`` and ``FsmSpec(...).hash()``
work for every spec instance without the caller doing any explicit
setup.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Aggregator — pure output folding
# ---------------------------------------------------------------------------
from ctxr.fsm.core.aggregator import (
    AggregatedAcrossStates,
    AggregatedLoop,
    aggregate_across_states,
    aggregate_loop_outputs,
)

# ---------------------------------------------------------------------------
# Engine — pure FSM runtime
# ---------------------------------------------------------------------------
from ctxr.fsm.core.engine import (
    EngineAdvanceResult,
    advance,
    build_brief,
    execute_inline,
    resolve_transition,
    run_post_validations,
    validate_output,
)

# ---------------------------------------------------------------------------
# Inline handler registry (W14a) — server-side deterministic state handlers
# ---------------------------------------------------------------------------
from ctxr.fsm.core.inline_registry import (
    InlineContext,
    InlineHandler,
    InlineHandlerRegistry,
    discover_handlers_via_entry_points,
    get_default_registry,
    lookup_with_discovery,
)

# ---------------------------------------------------------------------------
# Loop — iteration mechanics
# ---------------------------------------------------------------------------
from ctxr.fsm.core.loop import (
    LoopConfigError,
    outputs_path_for,
)
from ctxr.fsm.core.loop import decide as loop_decide

# ---------------------------------------------------------------------------
# Models — enums, spec primitives, engine value objects
# ---------------------------------------------------------------------------
from ctxr.fsm.core.models import (
    AllowedTools,
    Brief,
    CommitSignature,
    CommitToken,
    DeliveryStatus,
    EventKind,
    FsmSpec,
    InlineExecutionResult,
    InlineSpec,
    Loop,
    LoopDecision,
    PostValidationResult,
    PostValidationResultEntry,
    Predicate,
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
    VerifierSpec,
    VerifierVerdict,
    Worker,
    WorkerOutput,
)

# ---------------------------------------------------------------------------
# Predicates — sandboxed DSL evaluator
# ---------------------------------------------------------------------------
from ctxr.fsm.core.predicates import (
    PredicateEvalError,
    PredicateParseError,
    evaluate_expression,
    validate_expression,
)

# ---------------------------------------------------------------------------
# Protocols — typing surface for the W2 persistence layer
# ---------------------------------------------------------------------------
from ctxr.fsm.core.protocols import (
    EventBus,
    JournalProtocol,
    Lock,
    Repository,
)

# ---------------------------------------------------------------------------
# Spec — cross-cutting validation, canonical hashing, method attachment
# ---------------------------------------------------------------------------
from ctxr.fsm.core.spec import (
    FsmValidationResult,
    attach_methods,
    fsm_spec_hash,
    validate_fsm_spec,
)

# ---------------------------------------------------------------------------
# Verifier — adversarial verifier panel runtime (W12 layer 3)
# ---------------------------------------------------------------------------
from ctxr.fsm.core.verifier import (
    VerifierHandler,
    VerifierOutcome,
    VerifierVote,
    get_verifier_handler,
    run_verifier,
    set_verifier_handler,
)

# Ensure ``spec.validate()`` / ``spec.hash()`` are live for every
# ``FsmSpec`` instance produced anywhere in the process. ``attach_methods``
# is idempotent: a second invocation is a no-op.
attach_methods()


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
    # ── inline handler registry (W14a) ────────────────────────────────
    "InlineContext",
    "InlineExecutionResult",
    "InlineHandler",
    "InlineHandlerRegistry",
    "InlineSpec",
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
    "advance",
    "aggregate_across_states",
    "aggregate_loop_outputs",
    "attach_methods",
    "build_brief",
    "discover_handlers_via_entry_points",
    "evaluate_expression",
    "execute_inline",
    "fsm_spec_hash",
    "get_default_registry",
    "get_verifier_handler",
    "lookup_with_discovery",
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
