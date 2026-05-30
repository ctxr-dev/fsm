"""Adversarial verifier panel runtime (W12 layer 3).

This module provides the runtime that turns a declarative
:class:`~ctxr.fsm.core.models.VerifierSpec` (declared on a
:class:`~ctxr.fsm.core.models.State`) into a verdict over a worker's
outputs.

The shape is intentionally tiny and pluggable:

* :class:`VerifierVote` — one judge's opinion: ``passed`` or ``rejected``
  plus a short ``reason`` string.
* :class:`VerifierOutcome` — the panel aggregate: ``passed`` iff the
  number of ``passed`` votes meets the spec's ``majority_threshold``;
  carries the full vote list for forensic completeness.
* :func:`set_verifier_handler` / :func:`get_verifier_handler` — install
  a process-wide handler that knows how to actually run the panel (in
  the real system it dispatches LLM sub-agents out-of-process). When
  no handler is registered, :func:`run_verifier` falls back to a
  built-in *structural verifier* that simply re-checks the worker's
  response schema. This makes the verifier surface always live: every
  commit that lands on a state with ``verifier`` set goes through some
  form of independent re-check, even if the operator hasn't wired an
  LLM panel yet.
* :func:`run_verifier` — the entry point the commit pipeline calls. It
  picks the handler, asks for ``parallel_count`` votes, applies the
  majority rule, and returns a :class:`VerifierOutcome`.

Design notes
------------

* The handler signature is ``(verifier, brief, outputs) -> list[VerifierVote]``.
  Returning the full vote list (rather than just a verdict) keeps the
  audit trail intact — the commit pipeline persists both the aggregate
  outcome and every individual vote on the ``verifier_passed`` /
  ``verifier_rejected`` event payloads.
* The handler is registered as a *sync* callable. The commit pipeline
  itself runs synchronously inside the MCP tool body, so async would
  force an event-loop bridge. Real LLM-driven handlers can either block
  the worker thread (each FastMCP tool call is its own thread) or use
  ``asyncio.run`` internally — both are acceptable; this module does
  not impose a choice.
* The built-in structural verifier produces ``parallel_count`` votes
  (one per parallel slot) so the majority math is uniform across
  handlers; each vote independently reruns the schema check. In
  practice every vote agrees because the schema is deterministic, but
  emitting the full vote list keeps the downstream contract uniform.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ctxr.fsm.core.models import Brief, VerifierSpec, VerifierVerdict

__all__ = [
    "VerifierHandler",
    "VerifierOutcome",
    "VerifierVote",
    "get_verifier_handler",
    "run_verifier",
    "set_verifier_handler",
]


_VO_CFG = ConfigDict(strict=True, frozen=True, extra="forbid", populate_by_name=True)


class VerifierVote(BaseModel):
    """A single judge's verdict on a worker's outputs.

    ``verdict`` is constrained to the two terminal values the panel
    actually distinguishes — ``passed`` or ``rejected``. The broader
    :class:`VerifierVerdict` enum includes ``inconclusive`` for callers
    that want the third option, but the panel itself treats anything
    non-passing as a rejection so the majority math is unambiguous.

    ``reason`` is a short human-readable explanation; the commit
    pipeline persists it onto the ``verifier_passed`` /
    ``verifier_rejected`` event payload so the operator can see *why*
    each judge voted the way it did.
    """

    model_config = _VO_CFG

    verdict: Literal[VerifierVerdict.passed, VerifierVerdict.rejected]
    reason: str = ""


class VerifierOutcome(BaseModel):
    """Aggregate result of running a verifier panel."""

    model_config = _VO_CFG

    verdict: Literal[VerifierVerdict.passed, VerifierVerdict.rejected]
    votes: list[VerifierVote] = Field(default_factory=list)
    passed_count: int
    rejected_count: int
    majority_threshold: int
    parallel_count: int


# ---------------------------------------------------------------------------
# Handler registration
# ---------------------------------------------------------------------------


VerifierHandler = Callable[
    [VerifierSpec, Brief, dict[str, Any]],
    list[VerifierVote],
]
"""Signature of a pluggable verifier handler.

The handler receives the declarative :class:`VerifierSpec`, the
:class:`~ctxr.fsm.core.models.Brief` the worker just committed against,
and the raw ``outputs`` dict the worker produced. It MUST return a list
of :class:`VerifierVote` items — typically of length
``verifier.parallel_count`` — that :func:`run_verifier` then aggregates
under the spec's ``majority_threshold``.
"""


_REGISTERED_HANDLER: VerifierHandler | None = None


def set_verifier_handler(handler: VerifierHandler | None) -> None:
    """Install (or clear) the process-wide verifier handler.

    Pass ``None`` to uninstall a previously-registered handler and fall
    back to the built-in structural verifier. The setter is process-
    global because the FastMCP server runs every tool body inside the
    same Python process; per-call injection would require threading a
    handler reference through every layer between the MCP tool and the
    commit pipeline, which is more ceremony than the feature warrants.
    """
    global _REGISTERED_HANDLER
    _REGISTERED_HANDLER = handler


def get_verifier_handler() -> VerifierHandler | None:
    """Return the currently-registered verifier handler, or ``None``."""
    return _REGISTERED_HANDLER


# ---------------------------------------------------------------------------
# Built-in structural verifier
# ---------------------------------------------------------------------------


def _structural_verifier(
    verifier: VerifierSpec,
    brief: Brief,
    outputs: dict[str, Any],
) -> list[VerifierVote]:
    """Default panel that re-checks the worker's response schema.

    This is the "always present" verifier the brief calls out: no LLM,
    no I/O, just an independent re-application of the schema the worker
    was supposed to honour. It produces one vote per panel slot so the
    majority math is consistent across handler kinds.

    The verifier's own ``response_schema`` is intentionally NOT used as
    the gate here — that schema describes the shape of the *verifier
    panel's* output (e.g. ``{verdict, reason}``), not the worker's
    output. The thing we are re-checking is the worker's brief shape,
    which lives on ``brief.worker.response_schema`` (or, for a loop
    iteration, on ``brief.loop.worker.response_schema``).
    """
    # Pick the right response schema to re-check.  A state with no
    # worker / loop has nothing to structurally verify, so we accept by
    # default — the verifier still casts its parallel_count votes so the
    # outcome shape is uniform.
    schema = None
    if brief.loop is not None:
        schema = brief.loop.worker.response_schema
    elif brief.worker is not None:
        schema = brief.worker.response_schema

    if schema is None:
        return [
            VerifierVote(
                verdict=VerifierVerdict.passed,
                reason="no_response_schema_to_check",
            )
            for _ in range(verifier.parallel_count)
        ]

    valid, errors = schema.model_validate_json_payload(outputs)
    if valid:
        return [
            VerifierVote(
                verdict=VerifierVerdict.passed, reason="structural_check_ok"
            )
            for _ in range(verifier.parallel_count)
        ]
    reason = "; ".join(errors[:3]) if errors else "structural_check_failed"
    return [
        VerifierVote(verdict=VerifierVerdict.rejected, reason=reason)
        for _ in range(verifier.parallel_count)
    ]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _coerce_vote(raw: object) -> VerifierVote:
    """Tolerate dict-shaped votes returned by ad-hoc handlers.

    Real handlers should construct :class:`VerifierVote` directly, but
    we accept ``{"verdict": "...", "reason": "..."}`` dicts too because
    that's the lowest-common-denominator shape an LLM panel can emit.
    Anything unrecognised becomes a rejected vote so a malformed
    handler can never silently bypass the verifier.
    """
    if isinstance(raw, VerifierVote):
        return raw
    if isinstance(raw, dict):
        verdict_raw = raw.get("verdict")
        reason = raw.get("reason", "")
        if isinstance(reason, str):
            # Match each accepted vote value explicitly so the type
            # narrows down to the ``Literal[VerifierVerdict.passed,
            # VerifierVerdict.rejected]`` shape ``VerifierVote.verdict``
            # demands. ``VerifierVerdict(verdict)`` would type as the
            # full enum, which is wider than the model accepts.
            if verdict_raw == VerifierVerdict.passed.value:
                return VerifierVote(verdict=VerifierVerdict.passed, reason=reason)
            if verdict_raw == VerifierVerdict.rejected.value:
                return VerifierVote(verdict=VerifierVerdict.rejected, reason=reason)
        # ``inconclusive`` / unknown collapses to rejected — fail closed.
        return VerifierVote(
            verdict=VerifierVerdict.rejected,
            reason=f"malformed_vote: {raw!r}",
        )
    return VerifierVote(
        verdict=VerifierVerdict.rejected,
        reason=f"unknown_vote_type: {type(raw).__name__}",
    )


def run_verifier(
    verifier: VerifierSpec,
    brief: Brief,
    outputs: dict[str, Any],
) -> VerifierOutcome:
    """Run the verifier panel and return its aggregate outcome.

    Steps:

    1. Pick the handler (registered global, or the built-in structural
       fallback).
    2. Ask the handler for a vote list. Coerce any dict-shaped entries
       through :func:`_coerce_vote`.
    3. Apply the majority rule: the panel passes iff
       ``passed_count >= verifier.majority_threshold``.

    Returns a :class:`VerifierOutcome` carrying both the aggregate
    verdict and the full vote list so the commit pipeline can persist
    the forensic trail.
    """
    handler = _REGISTERED_HANDLER or _structural_verifier

    raw_votes = handler(verifier, brief, outputs)
    votes = [_coerce_vote(v) for v in raw_votes]

    passed = sum(1 for v in votes if v.verdict is VerifierVerdict.passed)
    rejected = sum(1 for v in votes if v.verdict is VerifierVerdict.rejected)
    aggregate: Literal[VerifierVerdict.passed, VerifierVerdict.rejected] = (
        VerifierVerdict.passed
        if passed >= verifier.majority_threshold
        else VerifierVerdict.rejected
    )

    return VerifierOutcome(
        verdict=aggregate,
        votes=votes,
        passed_count=passed,
        rejected_count=rejected,
        majority_threshold=verifier.majority_threshold,
        parallel_count=verifier.parallel_count,
    )


# Re-export VerifierVerdict for convenience so callers can do
# ``from ctxr.fsm.core.verifier import VerifierVerdict`` instead of
# reaching back into ``models``.
_ = VerifierVerdict
