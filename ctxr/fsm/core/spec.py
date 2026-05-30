"""Structural validation and canonical hashing for :class:`FsmSpec`.

This module sits one layer above :mod:`ctxr.fsm.core.models`. The Pydantic
constructors in ``models.py`` already cover *schema* validation (types,
field shapes, enum membership, per-state invariants). What they cannot
see is the relational story between states: reachability from the entry
state, dangling transition targets, loop completion fields, and the
parsability of every embedded predicate expression.

The free functions here close that gap:

* :func:`validate_fsm_spec` runs a deterministic battery of
  cross-cutting checks and returns a typed
  :class:`FsmValidationResult` that callers can dispatch on.
* :func:`fsm_spec_hash` produces a stable SHA-256 hex digest over the
  canonical JSON serialisation of the spec. This is the same hash the
  SQLite persistence layer stores in ``fsm_specs.hash`` to detect
  drift between an in-memory spec and the version recorded against a
  run.

To keep the ergonomic ``spec.validate()`` / ``spec.hash()`` surface
described in the workstream plan without modifying ``models.py`` (which
exposes a frozen, dependency-free domain layer), this module ships an
:func:`attach_methods` shim that binds the two free functions as
instance methods on :class:`FsmSpec`. It is invoked automatically at
import time. Importing ``ctxr.fsm.core.spec`` anywhere in the process
is therefore sufficient to make ``spec.validate()`` and ``spec.hash()``
work for every :class:`FsmSpec` instance, present or future.

Predicate parsability checks are *opportunistic*. They use
:func:`ctxr.fsm.core.predicates.validate_expression` when that module
is importable; if the predicates module is not yet on the path (it is
landed as a sibling W1 task), predicate checks are silently skipped and
the rest of the validation still runs. This keeps the validator useful
even in partially-bootstrapped environments such as test fixtures or
isolated smoke checks.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ctxr.fsm.core.models import FsmSpec, Predicate, State, Transition

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


class FsmValidationResult(BaseModel):
    """Structured outcome of :func:`validate_fsm_spec`.

    Attributes
    ----------
    valid:
        ``True`` iff no errors were collected during validation.
    errors:
        Human-readable error messages, one per detected problem.
    unreachable_states:
        Ids of states declared in :attr:`FsmSpec.states` that BFS
        from :attr:`FsmSpec.entry` did not reach.
    dangling_transitions:
        Pairs of ``(from_state_id, to_state_id)`` for transitions that
        reference an unknown target state.
    invalid_predicates:
        Pairs of ``(location, expression)`` for predicate expressions
        that failed to parse. ``location`` is a short breadcrumb such
        as ``"state:plan/transition[0].when"`` or
        ``"state:plan/post_validations[1]"``.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    valid: bool
    errors: list[str] = Field(default_factory=list)
    unreachable_states: list[str] = Field(default_factory=list)
    dangling_transitions: list[tuple[str, str]] = Field(default_factory=list)
    invalid_predicates: list[tuple[str, str]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _canonical_json_bytes(obj: Any) -> bytes:
    """Serialise ``obj`` to deterministic canonical JSON bytes.

    Uses ``sort_keys=True`` and the most compact separators so the
    output is byte-identical for inputs that differ only in whitespace
    or key order. This mirrors the helper used inside
    :mod:`ctxr.fsm.core.models` for commit-signature hashing.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _resolve_predicate_validator() -> Callable[[str], None] | None:
    """Return ``predicates.validate_expression`` if available, else ``None``.

    The predicates module is a sibling landed in the same workstream
    (W1). Importing it lazily keeps :mod:`ctxr.fsm.core.spec` safe to
    use in bootstrapping contexts where ``predicates.py`` may not yet
    exist on disk. When the module is present, the returned callable
    is expected to raise an exception on a malformed expression and
    return ``None`` (or any value, ignored here) on success.
    """
    try:
        from ctxr.fsm.core import predicates as _predicates_module
    except ImportError:
        return None

    validator = getattr(_predicates_module, "validate_expression", None)
    if validator is None or not callable(validator):
        return None
    # The sibling predicates module's public surface is open; we treat
    # ``validate_expression`` as ``Callable[[str], None]`` by contract.
    return validator  # type: ignore[no-any-return]


def _iter_predicate_expressions(
    spec: FsmSpec,
) -> list[tuple[str, str]]:
    """Collect every predicate expression in ``spec``.

    Returns a list of ``(location, expression)`` pairs. Locations are
    breadcrumbs of the form ``"state:<id>/transition[<i>].when"`` or
    ``"state:<id>/post_validations[<i>]"`` so error reports point at
    exactly the offending site.

    Transition ``when`` fields are only inspected when they carry a
    :class:`Predicate`; literal ``"always"`` / ``"otherwise"`` and
    judgement dicts have no DSL expression to parse.
    """
    out: list[tuple[str, str]] = []
    for state in spec.states:
        for idx, transition in enumerate(state.transitions):
            when = transition.when
            if isinstance(when, Predicate):
                out.append(
                    (f"state:{state.id}/transition[{idx}].when", when.expression)
                )
        for idx, predicate in enumerate(state.post_validations):
            out.append(
                (f"state:{state.id}/post_validations[{idx}]", predicate.expression)
            )
    return out


def _bfs_reachable(spec: FsmSpec) -> set[str]:
    """Return the set of state ids reachable from :attr:`FsmSpec.entry`.

    Traversal follows every declared transition's ``to`` target,
    regardless of guard kind: reachability is a *structural* property
    and does not depend on whether a given guard could ever fire at
    runtime. Unknown transition targets are silently ignored here and
    surface separately as dangling-transition errors so the two
    diagnostics stay independent.
    """
    known = {state.id for state in spec.states}
    by_id = {state.id: state for state in spec.states}

    reachable: set[str] = set()
    queue: deque[str] = deque()

    if spec.entry in known:
        reachable.add(spec.entry)
        queue.append(spec.entry)

    while queue:
        current = queue.popleft()
        state = by_id[current]
        for transition in state.transitions:
            target = transition.to
            if target in known and target not in reachable:
                reachable.add(target)
                queue.append(target)

    return reachable


def _check_loop_semantics(state: State) -> list[str]:
    """Return error strings for any loop-shape violations on ``state``.

    Pydantic's :class:`State` validator already raises on construction
    when ``loop.done_field`` is absent from the loop's worker response
    schema, so a well-constructed :class:`FsmSpec` cannot reach this
    function with a violation. The check is repeated here as a
    *defensive* invariant: it documents the rule, runs cheaply, and
    survives any future relaxation of the constructor-time validator.
    """
    errors: list[str] = []
    if state.loop is None:
        return errors

    loop = state.loop
    schema = (
        loop.worker.response_schema.schema_
        if loop.worker.response_schema is not None
        else None
    )
    properties: Any = None
    if isinstance(schema, dict):
        properties = schema.get("properties")

    if not isinstance(properties, dict) or loop.done_field not in properties:
        errors.append(
            f"state {state.id!r}: loop.done_field {loop.done_field!r} is not "
            "declared in loop.worker.response_schema.properties"
        )
    return errors


def _check_inline_semantics(state: State) -> list[str]:
    """Return error strings for any inline-shape violations on ``state``.

    Pydantic's :class:`State` validator already raises on construction
    when an inline state with outgoing transitions has no
    ``inline.response_schema`` (transition guards need a defined output
    shape to read), so a well-constructed :class:`FsmSpec` cannot
    reach this function with a violation. The check is repeated here
    as a *defensive* invariant: it documents the rule, runs cheaply,
    and survives any future relaxation of the constructor-time
    validator.

    Inline handler REGISTRATION is intentionally NOT validated here:
    handlers register at runtime via
    :class:`~ctxr.fsm.core.inline_registry.InlineHandlerRegistry` and
    a missing handler surfaces at engine advance time as a structured
    :class:`~ctxr.fsm.core.models.InlineExecutionResult` fault — not
    as a spec-load error. The contract is documented on
    :func:`validate_fsm_spec`.
    """
    errors: list[str] = []
    if state.inline is None:
        return errors
    if state.transitions and state.inline.response_schema is None:
        errors.append(
            f"state {state.id!r}: inline state with transitions must declare "
            "inline.response_schema so transition guards have a defined output "
            "shape to read"
        )
    return errors


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_fsm_spec(spec: FsmSpec) -> FsmValidationResult:
    """Run all post-construction validations on ``spec``.

    Performed checks, in order:

    1. **Reachability.** Every state in :attr:`FsmSpec.states` must be
       reachable from :attr:`FsmSpec.entry` via BFS over transitions.
    2. **Transition coverage.** Every :attr:`Transition.to` must
       reference an existing state id.
    3. **Loop semantics.** For each state with a :attr:`State.loop`,
       :attr:`Loop.done_field` must appear in the loop worker's
       response schema properties.
    4. **Inline semantics.** For each state with a
       :attr:`State.inline`, declaring outgoing transitions requires
       :attr:`~ctxr.fsm.core.models.InlineSpec.response_schema` to be
       set (so transition guards have a defined output shape to read).
       Inline states with no transitions are valid terminals.
    5. **Predicate parsability.** Every Predicate expression embedded
       in a transition guard or a post-validation must parse via
       :func:`ctxr.fsm.core.predicates.validate_expression`. If the
       predicates module is not importable in the current environment
       (e.g. early bootstrap), this check is skipped silently.

    What is **not** validated here:

    * Inline handler **registration** is intentionally out of scope.
      Inline handlers register at runtime against
      :class:`~ctxr.fsm.core.inline_registry.InlineHandlerRegistry`;
      a missing registration surfaces at engine advance time as a
      structured :class:`~ctxr.fsm.core.models.InlineExecutionResult`
      fault with ``fault_reason="inline_handler_unregistered: ..."``.
      Validating the registry at spec-load time would couple this
      module to runtime state and break the
      ``Project.register_spec`` → ``Project.register_inline_handlers``
      ordering that consumers are free to interleave.

    Returns a :class:`FsmValidationResult` whose ``valid`` field is
    ``True`` iff no errors were collected.
    """
    errors: list[str] = []
    unreachable_states: list[str] = []
    dangling_transitions: list[tuple[str, str]] = []
    invalid_predicates: list[tuple[str, str]] = []

    known_ids = {state.id for state in spec.states}

    # --- (1) Reachability ----------------------------------------------------
    reachable = _bfs_reachable(spec)
    missing = sorted(known_ids - reachable)
    if missing:
        unreachable_states.extend(missing)
        errors.append(
            "unreachable states from entry "
            f"{spec.entry!r}: {missing!r}"
        )

    # --- (2) Transition coverage --------------------------------------------
    for state in spec.states:
        for transition in state.transitions:
            if transition.to not in known_ids:
                pair = (state.id, transition.to)
                dangling_transitions.append(pair)
                errors.append(
                    f"state {state.id!r}: transition target {transition.to!r} "
                    "does not exist in spec.states"
                )

    # --- (3) Loop semantics -------------------------------------------------
    for state in spec.states:
        errors.extend(_check_loop_semantics(state))

    # --- (4) Inline semantics -----------------------------------------------
    for state in spec.states:
        errors.extend(_check_inline_semantics(state))

    # --- (5) Predicate parsability ------------------------------------------
    validator = _resolve_predicate_validator()
    if validator is not None:
        for location, expression in _iter_predicate_expressions(spec):
            try:
                validator(expression)
            except Exception as exc:
                invalid_predicates.append((location, expression))
                errors.append(
                    f"{location}: predicate expression {expression!r} failed to "
                    f"parse: {exc}"
                )

    return FsmValidationResult(
        valid=not errors,
        errors=errors,
        unreachable_states=unreachable_states,
        dangling_transitions=dangling_transitions,
        invalid_predicates=invalid_predicates,
    )


def fsm_spec_hash(spec: FsmSpec) -> str:
    """Return the SHA-256 hex digest of the canonical JSON of ``spec``.

    The spec is dumped via Pydantic in JSON mode with aliases applied
    (so ``ResponseSchema.schema_`` serialises as ``schema``) and
    ``None`` fields excluded. The resulting object is re-serialised to
    canonical JSON (sorted keys, compact separators) before hashing so
    the digest is stable across cosmetic re-orderings.

    Two specs with identical declarative content produce identical
    digests; any change in state ids, transitions, worker definitions,
    or schemas produces a different digest. This is the hash recorded
    against runs in the SQLite layer to detect spec drift on resume.
    """
    payload = spec.model_dump(mode="json", by_alias=True, exclude_none=True)
    canonical = _canonical_json_bytes(payload)
    return hashlib.sha256(canonical).hexdigest()


# ---------------------------------------------------------------------------
# Method binding shim
# ---------------------------------------------------------------------------

_METHODS_ATTACHED: bool = False


def attach_methods() -> None:
    """Bind :func:`validate_fsm_spec` and :func:`fsm_spec_hash` onto :class:`FsmSpec`.

    This installs ``FsmSpec.validate`` and ``FsmSpec.hash`` as regular
    instance methods so callers can write ``spec.validate()`` /
    ``spec.hash()`` in the ergonomic style the workstream plan
    describes, without modifying :mod:`ctxr.fsm.core.models`. The shim
    is idempotent: repeated invocations are no-ops.

    Frozen Pydantic models (with ``ConfigDict(frozen=True)``) forbid
    instance attribute mutation but not class-level method assignment,
    so this monkey-patch is safe.
    """
    global _METHODS_ATTACHED
    if _METHODS_ATTACHED:
        return

    def _validate(self: FsmSpec) -> FsmValidationResult:
        return validate_fsm_spec(self)

    def _hash(self: FsmSpec) -> str:
        return fsm_spec_hash(self)

    _validate.__doc__ = (
        "Run cross-cutting structural validations against this spec.\n\n"
        "See :func:`ctxr.fsm.core.spec.validate_fsm_spec` for the full "
        "list of checks. Returns a :class:`FsmValidationResult`."
    )
    _hash.__doc__ = (
        "Return the SHA-256 hex digest of this spec's canonical JSON.\n\n"
        "See :func:`ctxr.fsm.core.spec.fsm_spec_hash` for the exact "
        "serialisation rules."
    )

    # ``BaseModel.validate`` is a deprecated v1 classmethod still present
    # on the namespace; we deliberately shadow it with an instance method
    # of our own shape, which mypy cannot reconcile without help.
    FsmSpec.validate = _validate  # type: ignore[assignment,method-assign]
    FsmSpec.hash = _hash  # type: ignore[attr-defined]

    _METHODS_ATTACHED = True


# Attach on import so any module that touches ``spec.py`` activates the
# ergonomic surface immediately for the rest of the process. Idempotent.
attach_methods()


__all__ = [
    "FsmValidationResult",
    "attach_methods",
    "fsm_spec_hash",
    "validate_fsm_spec",
]


# Re-export the bound symbols so ``ctxr.fsm.core.spec`` importers do not
# need to remember the original :mod:`ctxr.fsm.core.models` import path.
__all__.extend(["FsmSpec", "State", "Transition"])
_ = (FsmSpec, State, Transition)  # quiet "imported but unused" linters
