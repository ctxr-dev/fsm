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
    resolve_transition,
    run_post_validations,
    validate_output,
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
    "VerifierSpec",
    "VerifierVerdict",
    "Worker",
    "WorkerOutput",
    "advance",
    "aggregate_across_states",
    "aggregate_loop_outputs",
    "attach_methods",
    "build_brief",
    "evaluate_expression",
    "fsm_spec_hash",
    "loop_decide",
    "outputs_path_for",
    "resolve_transition",
    "run_post_validations",
    "validate_expression",
    "validate_fsm_spec",
    "validate_output",
]
