"""Pure-function output aggregation for the ctxr FSM core.

This module provides deterministic, side-effect-free helpers for
aggregating worker output payloads in two distinct shapes:

* :func:`aggregate_loop_outputs` — folds the per-iteration outputs of a
  single looping state into a single ordered list of items (plus
  iteration metadata), so a loop's final exit payload can carry the
  merged record of every iteration that ran.

* :func:`aggregate_across_states` — folds the exit outputs of a sequence
  of *different* states into a single ordered list of items, recording
  which requested state ids were missing from the input map.

Both helpers are pure: they take plain Python values in and return a
frozen Pydantic model out, performing no I/O whatsoever. The SQLite
persistence layer (W2) is expected to adapt these helpers to the
materialised storage shape; nothing in this module knows about a
database, a filesystem, or a network.

The accompanying Pydantic models (:class:`AggregatedLoop` and
:class:`AggregatedAcrossStates`) follow the core's domain-model
convention: ``strict=True`` and ``frozen=True`` so the results are
immutable value objects that downstream code can safely share by
reference.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ctxr.fsm.core.models import State  # noqa: F401  (required import per spec)

__all__ = [
    "AggregatedAcrossStates",
    "AggregatedLoop",
    "aggregate_across_states",
    "aggregate_loop_outputs",
]


_AGG_CFG = ConfigDict(strict=True, frozen=True, extra="forbid")


class AggregatedLoop(BaseModel):
    """Result of folding a loop's per-iteration outputs.

    Attributes:
        items: The concatenation, in iteration order, of every
            ``payload[merge_field]`` list contributed by the iterations.
            Missing keys or non-list values contribute zero items.
        iteration_count: The number of iteration payloads that were
            considered (``len(iters)``).
        merged_length: ``len(items)``, surfaced explicitly so callers
            do not have to recompute it.
        iteration_meta: A per-iteration record carrying a 1-based
            ``iteration_n`` plus every key from the iteration payload
            *other than* ``merge_field``. Order matches the input.
    """

    model_config = _AGG_CFG

    items: list[Any] = Field(default_factory=list)
    iteration_count: int = 0
    merged_length: int = 0
    iteration_meta: list[dict[str, Any]] = Field(default_factory=list)


class AggregatedAcrossStates(BaseModel):
    """Result of folding the exit outputs of a sequence of states.

    Attributes:
        items: The concatenation, in ``state_ids`` order, of every
            ``states_outputs[state_id][merge_field]`` list. States whose
            ``merge_field`` is missing or not a list contribute zero
            items but are still counted in ``state_count``.
        from_states: The ``state_ids`` argument echoed back verbatim,
            so the caller has a self-contained record of what was
            requested.
        state_count: The number of requested state ids that *were*
            present in ``states_outputs`` (regardless of whether their
            ``merge_field`` was a list).
        missing_states: The subset of ``state_ids`` that were not keys
            of ``states_outputs``, preserving request order.
        merged_length: ``len(items)``.
        field: The ``merge_field`` that was aggregated, echoed back so
            the result is self-describing.
    """

    model_config = _AGG_CFG

    items: list[Any] = Field(default_factory=list)
    from_states: list[str] = Field(default_factory=list)
    state_count: int = 0
    missing_states: list[str] = Field(default_factory=list)
    merged_length: int = 0
    field: str = "findings"


def aggregate_loop_outputs(
    iters: list[dict[str, Any]],
    merge_field: str = "findings",
) -> AggregatedLoop:
    """Aggregate a loop's per-iteration output payloads.

    The input ``iters`` is the ordered list of per-iteration output
    dicts produced by a looping state's worker (already validated
    against its response schema). This helper:

    1. Concatenates ``iter[merge_field]`` from every iteration into a
       single ordered ``items`` list. A missing key or a non-list value
       contributes zero items (but the iteration is still counted and
       its other fields still surface in ``iteration_meta``).
    2. Records every *other* field of each iteration payload in
       ``iteration_meta``, prefixed with a 1-based ``iteration_n`` so
       downstream consumers can correlate metadata with the source
       iteration without re-reading the inputs.

    Args:
        iters: Ordered per-iteration output payloads.
        merge_field: The payload key whose list values are concatenated.
            Defaults to ``"findings"``.

    Returns:
        An immutable :class:`AggregatedLoop` carrying the merged
        ``items``, ``iteration_count``, ``merged_length``, and the
        per-iteration metadata records.
    """
    items: list[Any] = []
    iteration_meta: list[dict[str, Any]] = []
    for i, payload in enumerate(iters):
        value = payload.get(merge_field)
        if isinstance(value, list):
            items.extend(value)
        meta: dict[str, Any] = {"iteration_n": i + 1}
        for key, val in payload.items():
            if key == merge_field:
                continue
            meta[key] = val
        iteration_meta.append(meta)
    return AggregatedLoop(
        items=items,
        iteration_count=len(iters),
        merged_length=len(items),
        iteration_meta=iteration_meta,
    )


def aggregate_across_states(
    states_outputs: dict[str, dict[str, Any]],
    state_ids: list[str],
    merge_field: str = "findings",
) -> AggregatedAcrossStates:
    """Aggregate the exit outputs of a sequence of states.

    Walks ``state_ids`` in order; for each id, looks it up in
    ``states_outputs``:

    * If the id is *not* a key of ``states_outputs``, the id is
      appended to ``missing_states`` and contributes nothing.
    * If the id *is* a key, ``state_count`` is incremented. If the
      state's ``merge_field`` value is a list, its elements are
      appended to ``items``; otherwise the state still counts but
      contributes zero items.

    Args:
        states_outputs: Map from state id to that state's exit
            outputs dictionary.
        state_ids: The ordered sequence of state ids to aggregate.
        merge_field: The output key whose list values are concatenated.
            Defaults to ``"findings"``.

    Returns:
        An immutable :class:`AggregatedAcrossStates` carrying the
        merged ``items``, the original ``from_states`` list, the count
        of present states, the ordered list of ``missing_states``, the
        ``merged_length``, and the ``field`` that was aggregated.
    """
    items: list[Any] = []
    missing_states: list[str] = []
    state_count = 0
    for state_id in state_ids:
        if state_id not in states_outputs:
            missing_states.append(state_id)
            continue
        state_count += 1
        outputs = states_outputs[state_id]
        value = outputs.get(merge_field)
        if isinstance(value, list):
            items.extend(value)
    return AggregatedAcrossStates(
        items=items,
        from_states=list(state_ids),
        state_count=state_count,
        missing_states=missing_states,
        merged_length=len(items),
        field=merge_field,
    )
