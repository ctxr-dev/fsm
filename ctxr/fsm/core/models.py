"""Canonical Pydantic models, StrEnums, and typed shapes for the ctxr FSM core.

This module is the bedrock import surface for every other module in
``ctxr.fsm.core`` and its sibling subpackages. It defines:

* Enumerations for run/state/transition lifecycle and event taxonomy.
* Pure declarative spec types (``FsmSpec``, ``State``, ``Worker``,
  ``Loop``, ``Predicate``, ``Transition``, ``VerifierSpec``,
  ``ResponseSchema``).
* Engine-level value objects (``Brief``, ``WorkerOutput``,
  ``CommitSignature``, ``CommitToken``, ``ValidationResult``,
  ``PostValidationResult``, ``TransitionEvaluation``, ``LoopDecision``,
  ``RunCtx``, ``AllowedTools``).

The module deliberately has **no** runtime dependencies on the engine,
SQLite persistence, the predicate evaluator, the aggregator, or the
loop runtime. It MUST stay safe to import in any environment that has
``pydantic`` (and ``jsonschema`` for runtime payload validation in
``ResponseSchema.model_validate_json_payload``) installed.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from re import compile as re_compile
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _to_canonical_json(obj: Any) -> bytes:
    """Serialise ``obj`` to canonical JSON bytes.

    Canonical here means ``sort_keys=True`` and the most compact
    separators (no extraneous whitespace). The result is deterministic
    for any JSON-serialisable input and is the basis for content
    hashing across the system.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_hex(payload: bytes) -> str:
    """Return the lowercase hexadecimal SHA-256 digest of ``payload``."""
    return hashlib.sha256(payload).hexdigest()


_STATE_ID_RE = re_compile(r"^[a-z][a-z0-9_]*$")


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class RunStatus(StrEnum):
    """Lifecycle status of an FSM run."""

    in_progress = "in_progress"
    paused = "paused"
    faulted = "faulted"
    completed = "completed"
    aborted = "aborted"
    superseded = "superseded"
    drift_paused = "drift_paused"


class StateStatus(StrEnum):
    """Lifecycle status of a single state instance within a run."""

    entered = "entered"
    exited = "exited"
    faulted = "faulted"


class TransitionKind(StrEnum):
    """The shape of a transition guard."""

    always = "always"
    otherwise = "otherwise"
    deterministic = "deterministic"
    judgement = "judgement"


class EventKind(StrEnum):
    """Closed taxonomy of events recorded on the FSM event journal."""

    run_started = "run_started"
    run_paused = "run_paused"
    run_resumed = "run_resumed"
    run_completed = "run_completed"
    run_aborted = "run_aborted"
    state_entered = "state_entered"
    state_exited = "state_exited"
    state_faulted = "state_faulted"
    transition_taken = "transition_taken"
    worker_dispatched = "worker_dispatched"
    worker_committed = "worker_committed"
    validation_failed = "validation_failed"
    aggregate_built = "aggregate_built"
    journal_opened = "journal_opened"
    journal_finalised = "journal_finalised"
    journal_discarded = "journal_discarded"
    commit_signature_verified = "commit_signature_verified"
    commit_signature_mismatch = "commit_signature_mismatch"
    tool_call_observed = "tool_call_observed"
    drift_signal_recorded = "drift_signal_recorded"
    drift_pause_triggered = "drift_pause_triggered"
    verifier_passed = "verifier_passed"
    verifier_rejected = "verifier_rejected"
    commit_token_issued = "commit_token_issued"
    commit_token_consumed = "commit_token_consumed"
    commit_token_expired = "commit_token_expired"


class DeliveryStatus(StrEnum):
    """Status of an outbound notification / dispatch attempt."""

    pending = "pending"
    delivered = "delivered"
    acked = "acked"
    failed = "failed"


class SignalKind(StrEnum):
    """Closed taxonomy of drift signals the engine reacts to."""

    off_allowlist_tool_call = "off_allowlist_tool_call"
    repeated_validation_failed = "repeated_validation_failed"
    signature_mismatch = "signature_mismatch"
    verifier_rejection = "verifier_rejection"
    output_shape_near_miss = "output_shape_near_miss"
    idle_too_long = "idle_too_long"


class VerifierVerdict(StrEnum):
    """Outcome of running a verifier panel."""

    passed = "passed"
    rejected = "rejected"
    inconclusive = "inconclusive"


# ---------------------------------------------------------------------------
# Shared model configs
# ---------------------------------------------------------------------------

_DOMAIN_CFG = ConfigDict(strict=True, frozen=True, extra="forbid", populate_by_name=True)


# ---------------------------------------------------------------------------
# Spec primitives
# ---------------------------------------------------------------------------


class ResponseSchema(BaseModel):
    """A wrapper around a JSON Schema dict used by workers and verifiers.

    The JSON Schema lives under the ``schema`` field at the wire level
    (matching the language of JSON Schema itself), but because
    ``schema`` is a reserved attribute on ``pydantic.BaseModel`` we
    expose it internally as ``schema_`` with a serialisation alias.
    """

    model_config = ConfigDict(
        strict=True, frozen=True, extra="forbid", populate_by_name=True
    )

    schema_: dict[str, Any] = Field(alias="schema")

    @field_validator("schema_")
    @classmethod
    def _schema_is_dict(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value, dict):  # pragma: no cover - strict mode covers this
            raise TypeError("schema must be a JSON object (dict)")
        return value

    def model_validate_json_payload(self, payload: Any) -> tuple[bool, list[str]]:
        """Validate ``payload`` against this schema.

        Returns a tuple ``(valid, errors)`` where ``errors`` is a list
        of human-readable validation messages (empty when ``valid`` is
        ``True``).  The underlying validator is ``jsonschema``; if that
        package is not installed an ImportError will surface to the
        caller — by design, since the core package declares it as a
        runtime dependency.
        """
        try:
            from jsonschema import Draft202012Validator
        except ImportError as exc:  # pragma: no cover - dep is declared
            raise ImportError(
                "jsonschema is required for ResponseSchema.model_validate_json_payload"
            ) from exc

        validator = Draft202012Validator(self.schema_)
        errors = sorted(validator.iter_errors(payload), key=lambda e: list(e.absolute_path))
        if not errors:
            return True, []
        messages = [
            f"{'/'.join(str(p) for p in err.absolute_path) or '<root>'}: {err.message}"
            for err in errors
        ]
        return False, messages


class Worker(BaseModel):
    """A worker specification: who to call and with which prompt + schema."""

    model_config = _DOMAIN_CFG

    role: str
    prompt_template: str
    inputs: list[str] = Field(default_factory=list)
    response_schema: ResponseSchema | None = None

    @field_validator("role", "prompt_template")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must be a non-empty string")
        return value


class VerifierSpec(BaseModel):
    """A verifier panel specification."""

    model_config = _DOMAIN_CFG

    role: str
    prompt_template: str
    response_schema: ResponseSchema
    majority_threshold: int = 2
    parallel_count: int = 3

    @field_validator("role", "prompt_template")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must be a non-empty string")
        return value

    @field_validator("majority_threshold")
    @classmethod
    def _majority_at_least_one(cls, value: int) -> int:
        if value < 1:
            raise ValueError("majority_threshold must be >= 1")
        return value

    @model_validator(mode="after")
    def _parallel_at_least_majority(self) -> VerifierSpec:
        if self.parallel_count < self.majority_threshold:
            raise ValueError("parallel_count must be >= majority_threshold")
        return self


class Loop(BaseModel):
    """A bounded loop over a single worker until a done-field flips true."""

    model_config = _DOMAIN_CFG

    worker: Worker
    max_iterations: int = 30
    done_field: str
    iteration_outputs_dir: str | None = None

    @field_validator("max_iterations")
    @classmethod
    def _max_iter_at_least_one(cls, value: int) -> int:
        if value < 1:
            raise ValueError("max_iterations must be >= 1")
        return value

    @field_validator("done_field")
    @classmethod
    def _done_field_non_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("done_field must be a non-empty string")
        return value


class Predicate(BaseModel):
    """A guard expression in the predicate DSL.

    Only the source text is captured here; parsing/evaluation lives in
    the predicate module and operates on the canonical ``expression``.
    """

    model_config = _DOMAIN_CFG

    expression: str

    def __init__(self, expr: str | None = None, /, **data: Any) -> None:
        """Allow ``Predicate("foo == 1")`` sugar in addition to kwargs."""
        if expr is not None and "expression" not in data:
            data["expression"] = expr
        super().__init__(**data)

    @field_validator("expression")
    @classmethod
    def _non_empty_expression(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("expression must be a non-empty string")
        return value


class Transition(BaseModel):
    """A single transition out of a state, with a guard."""

    model_config = ConfigDict(
        strict=True, frozen=True, extra="forbid", populate_by_name=True, arbitrary_types_allowed=False
    )

    to: str
    when: Predicate | Literal["always", "otherwise"] | dict[str, Any]

    @field_validator("to")
    @classmethod
    def _to_state_shape(cls, value: str) -> str:
        if not _STATE_ID_RE.match(value):
            raise ValueError(
                "transition `to` must be a snake_case state id (^[a-z][a-z0-9_]*$)"
            )
        return value

    @model_validator(mode="before")
    @classmethod
    def _normalise_when(cls, data: Any) -> Any:
        """Normalise the various ``when`` shapes into the typed union.

        Accepted incoming shapes for ``when``:

        * The string literals ``"always"`` and ``"otherwise"``.
        * A ``Predicate`` instance (passes through).
        * A dict with ``{"kind": "always"}``, ``{"kind": "otherwise"}``,
          ``{"kind": "deterministic", "expression": "..."}``, or
          ``{"kind": "judgement", "criteria": "...",
          "evidence_required": bool}``.
        * A dict that is just ``{"expression": "..."}`` — treated as
          deterministic.

        Dicts are normalised but kept as dicts so the typed union
        captures them; only ``deterministic`` is lifted into a
        ``Predicate`` for convenience.
        """
        if not isinstance(data, dict):
            return data
        when = data.get("when")
        if when is None:
            return data

        # String literals pass through unchanged.
        if isinstance(when, str):
            if when not in ("always", "otherwise"):
                # treat any bare string as a deterministic expression
                data["when"] = Predicate(when)
            return data

        # Already a Predicate (or a mapping that pydantic will coerce
        # into one) — handle the dict-with-kind variants explicitly.
        if isinstance(when, dict):
            kind = when.get("kind")
            if kind == "always":
                data["when"] = "always"
                return data
            if kind == "otherwise":
                data["when"] = "otherwise"
                return data
            if kind == "deterministic":
                expr = when.get("expression")
                if not isinstance(expr, str) or not expr.strip():
                    raise ValueError(
                        "deterministic transition requires a non-empty `expression`"
                    )
                data["when"] = Predicate(expr)
                return data
            if kind == "judgement":
                criteria = when.get("criteria")
                if not isinstance(criteria, str) or not criteria.strip():
                    raise ValueError(
                        "judgement transition requires a non-empty `criteria`"
                    )
                evidence_required = when.get("evidence_required", False)
                if not isinstance(evidence_required, bool):
                    raise ValueError("evidence_required must be a bool")
                data["when"] = {
                    "kind": "judgement",
                    "criteria": criteria,
                    "evidence_required": evidence_required,
                }
                return data
            # Plain {"expression": "..."} -> deterministic Predicate.
            if "expression" in when and len(when) == 1:
                expr = when["expression"]
                if not isinstance(expr, str) or not expr.strip():
                    raise ValueError("expression must be a non-empty string")
                data["when"] = Predicate(expr)
                return data
            raise ValueError(
                f"unrecognised transition `when` dict shape: {sorted(when.keys())!r}"
            )

        return data


class State(BaseModel):
    """A single state in an FSM spec."""

    model_config = _DOMAIN_CFG

    id: str
    purpose: str = ""
    preconditions: list[str] = Field(default_factory=list)
    worker: Worker | None = None
    loop: Loop | None = None
    outputs: list[str] = Field(default_factory=list)
    post_validations: list[Predicate] = Field(default_factory=list)
    transitions: list[Transition] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    verifier: VerifierSpec | None = None

    @field_validator("id")
    @classmethod
    def _id_shape(cls, value: str) -> str:
        if not _STATE_ID_RE.match(value):
            raise ValueError("state id must match ^[a-z][a-z0-9_]*$")
        return value

    @model_validator(mode="after")
    def _consistency(self) -> State:
        if self.worker is not None and self.loop is not None:
            raise ValueError(
                f"state {self.id!r}: cannot have both `worker` and `loop` set"
            )
        if self.loop is not None:
            schema = (
                self.loop.worker.response_schema.schema_
                if self.loop.worker.response_schema is not None
                else None
            )
            properties = schema.get("properties") if isinstance(schema, dict) else None
            if not isinstance(properties, dict) or self.loop.done_field not in properties:
                raise ValueError(
                    f"state {self.id!r}: loop.done_field {self.loop.done_field!r} "
                    "must be declared in loop.worker.response_schema.properties"
                )
        return self


class FsmSpec(BaseModel):
    """A complete FSM specification (states, entry, version)."""

    model_config = _DOMAIN_CFG

    id: str
    version: int = 1
    entry: str
    states: list[State]

    @field_validator("id")
    @classmethod
    def _id_non_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("FsmSpec.id must be a non-empty string")
        return value

    @field_validator("version")
    @classmethod
    def _version_at_least_one(cls, value: int) -> int:
        if value < 1:
            raise ValueError("FsmSpec.version must be >= 1")
        return value

    @field_validator("states")
    @classmethod
    def _states_non_empty(cls, value: list[State]) -> list[State]:
        if not value:
            raise ValueError("FsmSpec.states must be a non-empty list")
        ids = [s.id for s in value]
        if len(ids) != len(set(ids)):
            raise ValueError("FsmSpec.states ids must be unique")
        return value

    @model_validator(mode="after")
    def _entry_is_known(self) -> FsmSpec:
        known = {s.id for s in self.states}
        if self.entry not in known:
            raise ValueError(
                f"FsmSpec.entry {self.entry!r} is not present in states {sorted(known)!r}"
            )
        return self

    def get_state(self, state_id: str) -> State:
        """Return the ``State`` with the given id, or raise ``KeyError``."""
        for state in self.states:
            if state.id == state_id:
                return state
        raise KeyError(state_id)


# ---------------------------------------------------------------------------
# Engine-side value objects
# ---------------------------------------------------------------------------


class Brief(BaseModel):
    """The work brief handed to a worker for one state entry / iteration."""

    model_config = _DOMAIN_CFG

    run_id: uuid.UUID
    fsm_id: str
    state: str
    purpose: str
    preconditions: list[str]
    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs_expected: list[str]
    post_validations: list[Predicate] = Field(default_factory=list)
    transitions: list[Transition]
    has_worker: bool
    has_loop: bool
    allowed_tools: list[str] = Field(default_factory=list)
    worker: Worker | None = None
    loop: Loop | None = None
    iteration_n: int | None = None
    outputs_path: str | None = None
    brief_id: uuid.UUID


class WorkerOutput(BaseModel):
    """The structured output a worker returns at commit time."""

    model_config = _DOMAIN_CFG

    outputs: dict[str, Any]
    signature: str | None = None


class AllowedTools(BaseModel):
    """A tiny wrapper around the capability surface offered to a worker."""

    model_config = _DOMAIN_CFG

    tools: list[str] = Field(default_factory=list)


class CommitSignature(BaseModel):
    """A cryptographic commitment binding a brief to its outputs."""

    model_config = _DOMAIN_CFG

    brief_id: uuid.UUID
    inputs_hash: str
    outputs_hash: str
    session_id: str
    signature: str

    @classmethod
    def compute(
        cls,
        brief_id: uuid.UUID,
        inputs: Any,
        outputs: Any,
        session_id: str,
    ) -> CommitSignature:
        """Compute a deterministic ``CommitSignature`` from raw values.

        Both ``inputs`` and ``outputs`` are canonicalised to JSON
        (sorted keys, compact separators) and hashed with SHA-256.
        The final ``signature`` field hashes the four components
        together so any single field change invalidates it.
        """
        inputs_hash = _sha256_hex(_to_canonical_json(inputs))
        outputs_hash = _sha256_hex(_to_canonical_json(outputs))
        envelope = {
            "brief_id": str(brief_id),
            "inputs_hash": inputs_hash,
            "outputs_hash": outputs_hash,
            "session_id": session_id,
        }
        signature = _sha256_hex(_to_canonical_json(envelope))
        return cls(
            brief_id=brief_id,
            inputs_hash=inputs_hash,
            outputs_hash=outputs_hash,
            session_id=session_id,
            signature=signature,
        )


class CommitToken(BaseModel):
    """A short-lived single-use token authorising a transition commit."""

    model_config = _DOMAIN_CFG

    token: uuid.UUID
    run_id: uuid.UUID
    state_id: str
    expected_next_state: str
    expires_at: datetime

    @classmethod
    def issue(
        cls,
        run_id: uuid.UUID,
        state_id: str,
        expected_next_state: str,
        ttl_seconds: int = 60,
    ) -> CommitToken:
        """Mint a new commit token expiring ``ttl_seconds`` from now (UTC)."""
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be >= 1")
        expires_at = datetime.now(tz=UTC) + timedelta(seconds=ttl_seconds)
        return cls(
            token=uuid.uuid4(),
            run_id=run_id,
            state_id=state_id,
            expected_next_state=expected_next_state,
            expires_at=expires_at,
        )


class ValidationResult(BaseModel):
    """The outcome of a single shape/schema validation."""

    model_config = _DOMAIN_CFG

    valid: bool
    errors: list[str] = Field(default_factory=list)


class PostValidationResultEntry(BaseModel):
    """A single entry in a post-validation report."""

    model_config = _DOMAIN_CFG

    check: str
    expression: str
    result: bool
    error: str | None = None


class PostValidationResult(BaseModel):
    """The aggregate result of running every post-validation predicate."""

    model_config = _DOMAIN_CFG

    valid: bool
    results: list[PostValidationResultEntry]


class TransitionEvaluation(BaseModel):
    """The outcome of evaluating one transition guard at exit time."""

    model_config = _DOMAIN_CFG

    to: str
    when: Any
    result: bool
    expression: str | None = None
    error: str | None = None
    kind: str | None = None
    criteria: str | None = None


class LoopDecision(BaseModel):
    """Decision made after one iteration of a loop body."""

    model_config = _DOMAIN_CFG

    is_loop: bool
    terminate: bool = False
    reason: Literal["done_field", "max_iterations"] | None = None
    iteration_n: int | None = None


class RunCtx(BaseModel):
    """Lightweight runtime context threaded through engine operations."""

    model_config = ConfigDict(strict=True, extra="forbid")

    run_id: uuid.UUID
    fsm_id: str
    current_state: str
    iteration_n: int | None = None
    env: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "AllowedTools",
    # engine value objects
    "Brief",
    "CommitSignature",
    "CommitToken",
    "DeliveryStatus",
    "EventKind",
    "FsmSpec",
    "Loop",
    "LoopDecision",
    "PostValidationResult",
    "PostValidationResultEntry",
    "Predicate",
    # spec
    "ResponseSchema",
    "RunCtx",
    # enums
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
]
