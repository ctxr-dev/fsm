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
from typing import Any

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
_HANDLER_ID_RE = re_compile(r"^[a-z][a-z0-9_]*$")


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


class StateKind(StrEnum):
    """Derived kind of a :class:`State`, computed from which body fields are set.

    Closed vocabulary surfaced via :attr:`State.kind`; consumed by the
    engine, the SQLite materialisation layer, and the brief serialiser.
    Members carry the same wire strings the engine used pre-W14i so JSON
    payloads remain byte-identical.

    ``gate`` is the W23g cross-FSM kind: a state that pauses the run
    waiting for a value supplied from outside the run's own state
    environment (an LLM-supplied literal or a binding to another run's
    output). See ``ctxr/fsm/memory/GATE_CONTRACT.md`` for the protocol.
    """

    worker = "worker"
    loop = "loop"
    inline = "inline"
    gate = "gate"
    terminal = "terminal"


class GateSourceKind(StrEnum):
    """Where a gate state pulls its resolved value from.

    ``run_output``  — the value is read from another run's state output
                       via :class:`GateBinding`.
    ``llm_supplied`` — the value is provided by the operator / LLM at
                       resolve time and validated against the gate's
                       ``response_schema``.
    """

    run_output = "run_output"
    llm_supplied = "llm_supplied"


class InlineFaultReason(StrEnum):
    """Closed taxonomy of inline-handler failure modes.

    Surfaced on :attr:`InlineExecutionResult.fault_reason` so callers
    branch on a typed value rather than parsing free-form strings. Any
    additional human-readable diagnostic (the offending type name, the
    handler's exception message, the spec/handler key) goes on
    :attr:`InlineExecutionResult.fault_detail`.
    """

    unregistered = "unregistered"
    raised = "raised"
    bad_return_type = "bad_return_type"
    validation_failed = "validation_failed"
    post_validation_failed = "post_validation_failed"


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
    inline_executed = "inline_executed"
    inline_failed = "inline_failed"
    # W23g cross-FSM gates: gate_resolved when fsm.resolve_gate has
    # validated + landed a value; gate_resolution_failed for any error
    # envelope listed in GATE_CONTRACT.md; gate_binding_recorded when
    # a binding row lands in the gate_bindings table (source_kind
    # 'run_output' bindings only, since 'llm_supplied' resolutions
    # have no cross-run link to record).
    gate_resolved = "gate_resolved"
    gate_resolution_failed = "gate_resolution_failed"
    gate_binding_recorded = "gate_binding_recorded"


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


class LoopTerminationReason(StrEnum):
    """Why a loop body terminated.

    Surfaced on :attr:`LoopDecision.reason`; persisted on the loop
    state row so the dashboard can distinguish "the worker said it was
    done" from "the safety cap fired before the worker said so".
    """

    done_field = "done_field"
    max_iterations = "max_iterations"


class EngineAdvanceKind(StrEnum):
    """Discriminator for :class:`~ctxr.fsm.core.engine.EngineAdvanceResult`.

    The engine's high-level driver returns one of four results — this
    enum is the typed handle every downstream layer (MCP tool layer,
    SQLite repo, CLI) branches on.
    """

    advance = "advance"
    loop_continue = "loop_continue"
    terminal = "terminal"
    fault = "fault"


class JournalStatus(StrEnum):
    """Lifecycle status of a ``journal_txns`` row.

    Surfaced on :class:`~ctxr.fsm.sqlite.repos_locks_journal.JournalTxn`
    and on the API / MCP recovery surface. The lifecycle is strictly
    monotonic: ``pending → ready_to_finalise → finalised`` (with
    ``discard`` operations deleting the row outright rather than
    transitioning it to a fourth state).
    """

    pending = "pending"
    ready_to_finalise = "ready_to_finalise"
    finalised = "finalised"


class LockReleaseReason(StrEnum):
    """Outcome reason returned by :class:`LocksRepo.release`.

    Surfaced on :class:`~ctxr.fsm.sqlite.repos_locks_journal.ReleaseResult`
    so callers can branch on the typed value rather than match a
    free-form string. ``released`` is the success path; ``not_owner``
    and ``not_held`` are the two refusal paths the engine policy
    distinguishes.
    """

    released = "released"
    not_owner = "not_owner"
    not_held = "not_held"


class LockAcquireReason(StrEnum):
    """Outcome reason returned by :class:`LocksRepo.acquire`.

    Surfaced on :class:`~ctxr.fsm.sqlite.repos_locks_journal.LockResult`:

    * ``acquired`` — a fresh acquisition on a previously-unheld lock.
    * ``replaced_stale`` — we took over an expired lease from another
      session.
    * ``already_held_by_same_session`` — re-entrant acquire (the
      caller already held the lock; the row was refreshed in place).
    * ``held`` — a live, foreign lock that we did not displace; the
      caller did not acquire.
    """

    acquired = "acquired"
    replaced_stale = "replaced_stale"
    already_held_by_same_session = "already_held_by_same_session"
    held = "held"


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
    """A worker specification: who to call and with which prompt + schema.

    ``prompt_template_language`` is an optional, free-form format hint
    the consuming spec uses to tell downstream tooling how to render
    the prompt body. The FSM library itself treats ``prompt_template``
    as an opaque string regardless of the hint, but UI surfaces (the
    fsm-ui specDetail inspector, future spec-doc generators, etc.) can
    pick syntax highlighting / markdown rendering / etc. based on this
    declaration without resorting to content heuristics. Common
    values: ``"markdown"``, ``"jinja"``, ``"plain"``, ``"json"``. The
    field is intentionally a free string, not an enum, because the
    library shouldn't curate the universe of template formats consumers
    might use; consumers own the convention and the renderer that
    honours it.
    """

    model_config = _DOMAIN_CFG

    role: str
    prompt_template: str
    prompt_template_language: str | None = None
    inputs: list[str] = Field(default_factory=list)
    response_schema: ResponseSchema | None = None

    @field_validator("role", "prompt_template")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must be a non-empty string")
        return value

    @field_validator("prompt_template_language")
    @classmethod
    def _language_non_empty(cls, value: str | None) -> str | None:
        # None (= no hint) is legal. Empty/whitespace strings are not:
        # if a consumer cares enough to set the field, they must put a
        # real value in it. Catches typos like
        # ``prompt_template_language=""`` that would otherwise silently
        # behave the same as omitting the field.
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError(
                "prompt_template_language must be a non-empty string "
                "(or omitted entirely if no hint applies)"
            )
        return stripped


class VerifierSpec(BaseModel):
    """A verifier panel specification.

    Same ``prompt_template_language`` semantics as :class:`Worker`: an
    optional free-form format hint the consumer declares so renderers
    can pick the right view without content heuristics. See
    :class:`Worker` for the full contract.
    """

    model_config = _DOMAIN_CFG

    role: str
    prompt_template: str
    prompt_template_language: str | None = None
    response_schema: ResponseSchema
    majority_threshold: int = 2
    parallel_count: int = 3

    @field_validator("role", "prompt_template")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must be a non-empty string")
        return value

    @field_validator("prompt_template_language")
    @classmethod
    def _language_non_empty(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError(
                "prompt_template_language must be a non-empty string "
                "(or omitted entirely if no hint applies)"
            )
        return stripped

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


class InlineSpec(BaseModel):
    """A deterministic, server-side state handler specification.

    An inline state is the fourth state kind alongside ``worker``,
    ``loop``, and ``terminal``. Where a worker state pauses the engine
    so an out-of-process LLM (or any other dispatcher) can produce the
    state's outputs, an inline state runs a registered Python callable
    in-process during ``engine.advance()`` — in the SAME atomic
    transaction as the surrounding state transition, with no LLM round
    trip.

    Fields
    ------
    handler_id:
        Snake-case slug identifying which registered callable to invoke
        for this state. The engine looks this id up in the inline
        handler registry at advance time; registration is the consumer's
        responsibility and is NOT validated at spec-load time.
    response_schema:
        Optional JSON Schema (wrapped in :class:`ResponseSchema`) the
        handler's return dict is validated against. When the inline
        state has outgoing transitions, a schema is required so the
        transition guards have a defined output shape to read.
    post_validations:
        Optional :class:`Predicate` list evaluated against the handler's
        output. Same semantics as :attr:`State.post_validations`, except
        these are scoped to the inline handler's return value.
    purpose:
        Human-readable description shown in briefs/dashboards alongside
        the surrounding state's :attr:`State.purpose`.
    """

    model_config = _DOMAIN_CFG

    handler_id: str
    response_schema: ResponseSchema | None = None
    post_validations: list[Predicate] = Field(default_factory=list)
    purpose: str = ""

    @field_validator("handler_id")
    @classmethod
    def _handler_id_shape(cls, value: str) -> str:
        if not _HANDLER_ID_RE.match(value):
            raise ValueError(
                "InlineSpec.handler_id must match ^[a-z][a-z0-9_]*$"
            )
        return value


class GateBinding(BaseModel):
    """A single ``run_output`` binding consumed by a :class:`Gate`.

    A binding tells the engine which other run's state output should
    land under ``target_field`` in this run's environment when the gate
    resolves. ``source_run_id`` may be ``None`` at spec-author time and
    populated by the orchestrator at start_run time when the source run
    id is only known dynamically. ``source_spec_slug`` is an optional
    safety check: when set, the resolver refuses bindings whose source
    run's spec slug does not match.
    """

    model_config = _DOMAIN_CFG

    source_run_id: str | None = None
    source_spec_slug: str | None = None
    source_state_id: str
    source_field: str
    target_field: str

    @field_validator("source_state_id", "source_field", "target_field")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("GateBinding field must be a non-empty string")
        return value

    @field_validator("source_run_id", "source_spec_slug")
    @classmethod
    def _optional_non_empty(cls, value: str | None) -> str | None:
        # Optional identifiers: ``None`` keeps the "unset" semantics, but
        # an explicit empty/whitespace-only string is ambiguous and would
        # silently bypass the spec-slug safety check or produce a binding
        # against a phantom run id. Reject at the boundary.
        if value is not None and not value.strip():
            raise ValueError(
                "GateBinding optional field, when provided, must be a "
                "non-empty string"
            )
        return value


class Gate(BaseModel):
    """A cross-FSM gate body for a :class:`State`.

    Sets a state's kind to :attr:`StateKind.gate`. The engine pauses
    the run when it enters a gate state and waits for an explicit
    ``fsm.resolve_gate`` call. The resolved value is validated against
    :attr:`response_schema` and lands in the run's environment under
    the gate's first declared output (or under each binding's
    ``target_field`` when ``source_kind == run_output``).

    ``max_age_ms`` is optional and applies only to ``run_output``
    sources: if set, the resolver rejects sources whose state's
    ``exited_at`` is older than the window with
    ``error: gate_source_stale`` (see GATE_CONTRACT.md).
    """

    model_config = _DOMAIN_CFG

    source_kind: GateSourceKind
    response_schema: ResponseSchema
    bindings: list[GateBinding] = Field(default_factory=list)
    max_age_ms: int | None = None

    @field_validator("max_age_ms")
    @classmethod
    def _max_age_positive(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("Gate.max_age_ms must be a positive integer")
        return value

    @model_validator(mode="after")
    def _bindings_match_source_kind(self) -> Gate:
        if self.source_kind is GateSourceKind.llm_supplied and self.bindings:
            raise ValueError(
                "Gate.bindings must be empty when source_kind=llm_supplied"
            )
        return self


class Transition(BaseModel):
    """A single transition out of a state, with a guard."""

    model_config = ConfigDict(
        strict=True, frozen=True, extra="forbid", populate_by_name=True, arbitrary_types_allowed=False
    )

    to: str
    when: Predicate | TransitionKind | dict[str, Any]

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

        * The string literals ``"always"`` and ``"otherwise"``
          (normalised into :class:`TransitionKind` members; any other
          bare string is lifted into a :class:`Predicate`).
        * A :class:`TransitionKind` member (passes through).
        * A :class:`Predicate` instance (passes through).
        * A dict with ``{"kind": "always"}``, ``{"kind": "otherwise"}``,
          ``{"kind": "deterministic", "expression": "..."}``, or
          ``{"kind": "judgement", "criteria": "...",
          "evidence_required": bool}``.
        * A dict that is just ``{"expression": "..."}`` — treated as
          deterministic.

        Judgement dicts are normalised but kept as dicts so the typed
        union captures the criteria + evidence_required payload;
        ``always`` / ``otherwise`` resolve to enum members and
        ``deterministic`` is lifted into a :class:`Predicate`.
        """
        if not isinstance(data, dict):
            return data
        when = data.get("when")
        if when is None:
            return data

        # Bare strings — map the two enum-vocabulary values onto enum
        # members, lift anything else into a deterministic Predicate.
        if isinstance(when, str) and not isinstance(when, TransitionKind):
            if when == TransitionKind.always.value:
                data["when"] = TransitionKind.always
                return data
            if when == TransitionKind.otherwise.value:
                data["when"] = TransitionKind.otherwise
                return data
            data["when"] = Predicate(when)
            return data

        # Already a TransitionKind member — only ``always`` and
        # ``otherwise`` are valid bare guards. ``deterministic`` and
        # ``judgement`` carry payloads (expression / criteria) and MUST
        # come in as the dict form below; passing them bare would silently
        # accept a guard with no expression and fault later in the engine.
        # Reject at the boundary with a message that points the author
        # at the dict form.
        if isinstance(when, TransitionKind):
            if when not in (TransitionKind.always, TransitionKind.otherwise):
                raise ValueError(
                    f"Transition.when={when.value!r} cannot be used as a bare "
                    "guard. Only `always` and `otherwise` are valid bare "
                    "TransitionKind members; for `deterministic` use "
                    "`{\"kind\": \"deterministic\", \"expression\": \"...\"}` "
                    "and for `judgement` use "
                    "`{\"kind\": \"judgement\", \"criteria\": \"...\", "
                    "\"evidence_required\": <bool>}`."
                )
            return data

        # Already a Predicate (or a mapping that pydantic will coerce
        # into one) — handle the dict-with-kind variants explicitly.
        if isinstance(when, dict):
            kind = when.get("kind")
            if kind == TransitionKind.always.value:
                data["when"] = TransitionKind.always
                return data
            if kind == TransitionKind.otherwise.value:
                data["when"] = TransitionKind.otherwise
                return data
            if kind == TransitionKind.deterministic.value:
                expr = when.get("expression")
                if not isinstance(expr, str) or not expr.strip():
                    raise ValueError(
                        "deterministic transition requires a non-empty `expression`"
                    )
                data["when"] = Predicate(expr)
                return data
            if kind == TransitionKind.judgement.value:
                criteria = when.get("criteria")
                if not isinstance(criteria, str) or not criteria.strip():
                    raise ValueError(
                        "judgement transition requires a non-empty `criteria`"
                    )
                evidence_required = when.get("evidence_required", False)
                if not isinstance(evidence_required, bool):
                    raise ValueError("evidence_required must be a bool")
                data["when"] = {
                    "kind": TransitionKind.judgement.value,
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
    inline: InlineSpec | None = None
    gate: Gate | None = None
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

    @property
    def kind(self) -> StateKind:
        """Return the derived kind of this state.

        The kind is derived from which body fields are set, not stored:

        * :attr:`StateKind.loop`     - :attr:`loop` is non-None.
        * :attr:`StateKind.inline`   - :attr:`inline` is non-None
          (and :attr:`loop` is None).
        * :attr:`StateKind.gate`     - :attr:`gate` is non-None
          (and :attr:`loop` / :attr:`inline` are None). Exclusive with
          worker / loop / inline; enforced by the model_validator.
        * :attr:`StateKind.worker`   - :attr:`worker` is non-None
          (and :attr:`loop` / :attr:`inline` / :attr:`gate` are None).
        * :attr:`StateKind.terminal` - all four body fields are None
          AND :attr:`transitions` is empty.

        A state with no body fields but a non-empty :attr:`transitions`
        list is structurally invalid (a "pass-through" worker would
        need somebody to produce its outputs) and raises ``ValueError``.

        ``StateKind`` members are ``str`` subclasses (StrEnum), so any
        legacy ``state.kind == "worker"`` comparison continues to work
        — the property's typed return value is the canonical handle and
        new call sites should compare against the enum directly.
        """
        if self.loop is not None:
            return StateKind.loop
        if self.inline is not None:
            return StateKind.inline
        if self.gate is not None:
            return StateKind.gate
        if self.worker is not None:
            return StateKind.worker
        if not self.transitions:
            return StateKind.terminal
        raise ValueError(
            f"state {self.id!r}: terminal-by-content but has transitions; "
            "set a worker / loop / inline / gate body or remove the transitions"
        )

    @model_validator(mode="after")
    def _consistency(self) -> State:
        if self.worker is not None and self.loop is not None:
            raise ValueError(
                f"state {self.id!r}: cannot have both `worker` and `loop` set"
            )
        if self.inline is not None and self.worker is not None:
            raise ValueError(
                f"state {self.id!r}: cannot have both `inline` and `worker` set"
            )
        if self.inline is not None and self.loop is not None:
            raise ValueError(
                f"state {self.id!r}: cannot have both `inline` and `loop` set"
            )
        # W23g: gate body is exclusive with every other body kind. The
        # engine pauses on a gate the way it pauses on a worker, but
        # the resolver is fsm.resolve_gate, not fsm.commit_outputs;
        # combining bodies would make the brief shape ambiguous.
        if self.gate is not None and (
            self.worker is not None
            or self.loop is not None
            or self.inline is not None
        ):
            raise ValueError(
                f"state {self.id!r}: `gate` cannot be combined with "
                "`worker`, `loop`, or `inline`"
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
        if (
            self.inline is not None
            and self.transitions
            and self.inline.response_schema is None
        ):
            raise ValueError(
                f"state {self.id!r}: inline state with transitions must declare "
                "inline.response_schema so transition guards have a defined "
                "output shape to read"
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
    # W23g cross-FSM gates: when the current state is a gate, the
    # brief carries the Gate body (response_schema + bindings +
    # source_kind) instead of a Worker. has_worker and has_loop both
    # stay False; consumers branch on `gate is not None` to switch
    # from the commit_outputs path to the resolve_gate path.
    gate: Gate | None = None
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


class InlineExecutionResult(BaseModel):
    """The outcome of executing an inline state handler.

    Returned by :func:`ctxr.fsm.core.engine.execute_inline`. The shape
    is a single envelope (not a discriminated union) so callers can
    branch on the ``ok`` flag and consume
    :attr:`fault_reason` / :attr:`fault_detail` / :attr:`outputs`
    accordingly:

    * ``ok=True``  — the handler ran, returned a dict, validated
      against any declared response schema and post-validations. The
      handler's return value is in :attr:`outputs`; :attr:`fault_reason`
      and :attr:`fault_detail` are ``None``.
    * ``ok=False`` — the handler did not produce usable outputs.
      :attr:`fault_reason` is one of the :class:`InlineFaultReason`
      members (``unregistered``, ``raised``, ``bad_return_type``,
      ``validation_failed``, ``post_validation_failed``).
      :attr:`fault_detail` carries the human-readable diagnostic (the
      raising exception's ``str()``, the offending return type's name,
      the missing handler's ``(spec_id, handler_id)`` key — whatever the
      consumer would want to log).

    :attr:`validation` always carries the schema-validation report
    (vacuously valid when no schema is declared);
    :attr:`post_validations` is populated only when the inline spec
    declared post-validations.

    The model is strict + frozen + extra-forbid like the surrounding
    domain envelopes; mis-typed kwargs surface immediately.

    .. note::
       The W14i refactor split the legacy free-form ``fault_reason``
       string (e.g. ``"inline_handler_raised: RuntimeError: boom"``) into
       a typed :class:`InlineFaultReason` member plus a separate
       :attr:`fault_detail` payload. Wire JSON consumers that previously
       grepped for the ``"inline_handler_"`` prefix should switch to the
       typed field; the legacy prefix is no longer emitted.
    """

    model_config = _DOMAIN_CFG

    handler_id: str
    ok: bool
    outputs: dict[str, Any] = Field(default_factory=dict)
    validation: ValidationResult
    post_validations: PostValidationResult | None = None
    fault_reason: InlineFaultReason | None = None
    fault_detail: str | None = None


class TransitionEvaluation(BaseModel):
    """The outcome of evaluating one transition guard at exit time.

    ``kind`` is a plain string (a :class:`TransitionKind` member's
    ``.value``, or the defensive sentinel ``"unknown"`` when the engine
    could not resolve the guard shape), or ``None`` when the guard was
    missing entirely. It is intentionally NOT typed as
    :class:`TransitionKind` because the ``"unknown"`` sentinel must be
    representable (an unknown guard is a programming bug surfaced via
    :attr:`error`, not a new enum entry), and pinning the field to the
    enum would force every consumer through ``TransitionKind(value)``
    coercion at the boundary. Callers that need an enum member should
    use ``TransitionKind(eval.kind) if eval.kind in {m.value for m in
    TransitionKind} else None`` (or guard on ``eval.kind != "unknown"``
    first); identity checks like ``eval.kind is TransitionKind.always``
    will always be ``False`` because the field carries the string value,
    not the member.
    """

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
    reason: LoopTerminationReason | None = None
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
    "EngineAdvanceKind",
    "EventKind",
    "FsmSpec",
    "InlineExecutionResult",
    "InlineFaultReason",
    "InlineSpec",
    "JournalStatus",
    "LockAcquireReason",
    "LockReleaseReason",
    "Loop",
    "LoopDecision",
    "LoopTerminationReason",
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
    "StateKind",
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
