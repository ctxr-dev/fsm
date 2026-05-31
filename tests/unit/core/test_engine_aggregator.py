"""Unit tests for ctxr.fsm.core.aggregator.

Covers:
* ``aggregate_loop_outputs``: empty iters => empty items; N iters
  concatenate in order; missing merge_field contributes []; idempotent.
* ``aggregate_across_states``: preserves order of items per requested
  state ids; missing states reported (in request order).
"""

from __future__ import annotations

from ctxr.fsm.core.aggregator import (
    AggregatedAcrossStates,
    AggregatedLoop,
    aggregate_across_states,
    aggregate_loop_outputs,
)

# ---------------------------------------------------------------------------
# aggregate_loop_outputs
# ---------------------------------------------------------------------------


def test_aggregate_loop_outputs_empty_iters_returns_empty_items() -> None:
    """No iterations means no items, no metadata, zero counts."""
    result = aggregate_loop_outputs([])
    assert isinstance(result, AggregatedLoop)
    assert result.items == []
    assert result.iteration_count == 0
    assert result.merged_length == 0
    assert result.iteration_meta == []


def test_aggregate_loop_outputs_concatenates_n_iters_in_order() -> None:
    """N iterations concatenate their merge_field lists in input order."""
    iters = [
        {"findings": ["a1", "a2"], "note": "first"},
        {"findings": ["b1"], "note": "second"},
        {"findings": ["c1", "c2", "c3"], "note": "third"},
    ]
    result = aggregate_loop_outputs(iters)
    assert result.items == ["a1", "a2", "b1", "c1", "c2", "c3"]
    assert result.iteration_count == 3
    assert result.merged_length == 6
    assert result.iteration_meta == [
        {"iteration_n": 1, "note": "first"},
        {"iteration_n": 2, "note": "second"},
        {"iteration_n": 3, "note": "third"},
    ]


def test_aggregate_loop_outputs_missing_merge_field_contributes_empty() -> None:
    """Iterations lacking the merge_field key contribute zero items but still count."""
    iters = [
        {"findings": ["a"]},
        {"note": "no findings here"},  # missing merge_field
        {"findings": ["b"]},
    ]
    result = aggregate_loop_outputs(iters)
    assert result.items == ["a", "b"]
    assert result.iteration_count == 3
    assert result.merged_length == 2
    # Middle iteration is still recorded in meta, just with no findings entry.
    assert result.iteration_meta == [
        {"iteration_n": 1},
        {"iteration_n": 2, "note": "no findings here"},
        {"iteration_n": 3},
    ]


def test_aggregate_loop_outputs_non_list_merge_field_contributes_empty() -> None:
    """A non-list merge_field value contributes zero items but still counts."""
    iters = [
        {"findings": ["a"]},
        {"findings": "not-a-list"},
        {"findings": None},
        {"findings": ["b"]},
    ]
    result = aggregate_loop_outputs(iters)
    assert result.items == ["a", "b"]
    assert result.iteration_count == 4
    assert result.merged_length == 2


def test_aggregate_loop_outputs_is_idempotent() -> None:
    """Calling the helper twice on the same input yields equal results."""
    iters = [
        {"findings": ["x"], "k": 1},
        {"findings": ["y", "z"], "k": 2},
    ]
    first = aggregate_loop_outputs(iters)
    second = aggregate_loop_outputs(iters)
    assert first == second
    # And re-running it produces the same items list value as well.
    assert first.items == second.items
    assert first.iteration_meta == second.iteration_meta


def test_aggregate_loop_outputs_custom_merge_field() -> None:
    """A non-default merge_field is honoured; old default key becomes metadata."""
    iters = [
        {"hits": [1, 2], "findings": ["meta-a"]},
        {"hits": [3], "findings": ["meta-b"]},
    ]
    result = aggregate_loop_outputs(iters, merge_field="hits")
    assert result.items == [1, 2, 3]
    assert result.iteration_count == 2
    assert result.merged_length == 3
    # "findings" is no longer the merge field, so it surfaces in metadata.
    assert result.iteration_meta == [
        {"iteration_n": 1, "findings": ["meta-a"]},
        {"iteration_n": 2, "findings": ["meta-b"]},
    ]


def test_aggregate_loop_outputs_result_is_frozen() -> None:
    """AggregatedLoop is an immutable value object (frozen=True)."""
    result = aggregate_loop_outputs([{"findings": ["a"]}])
    # Pydantic v2 raises ValidationError on assignment to a frozen model.
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        result.iteration_count = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# aggregate_across_states
# ---------------------------------------------------------------------------


def test_aggregate_across_states_preserves_request_order() -> None:
    """Items concatenate in the order of state_ids, not dict insertion order."""
    states_outputs = {
        "state_b": {"findings": ["b1", "b2"]},
        "state_a": {"findings": ["a1"]},
        "state_c": {"findings": ["c1", "c2"]},
    }
    result = aggregate_across_states(
        states_outputs,
        state_ids=["state_a", "state_b", "state_c"],
    )
    assert isinstance(result, AggregatedAcrossStates)
    assert result.items == ["a1", "b1", "b2", "c1", "c2"]
    assert result.from_states == ["state_a", "state_b", "state_c"]
    assert result.state_count == 3
    assert result.missing_states == []
    assert result.merged_length == 5
    assert result.field == "findings"


def test_aggregate_across_states_reports_missing_states_in_request_order() -> None:
    """Requested state ids absent from states_outputs are reported in request order."""
    states_outputs = {
        "present_1": {"findings": ["p1"]},
        "present_2": {"findings": ["p2"]},
    }
    result = aggregate_across_states(
        states_outputs,
        state_ids=["missing_a", "present_1", "missing_b", "present_2", "missing_c"],
    )
    assert result.items == ["p1", "p2"]
    assert result.from_states == [
        "missing_a",
        "present_1",
        "missing_b",
        "present_2",
        "missing_c",
    ]
    assert result.state_count == 2
    assert result.missing_states == ["missing_a", "missing_b", "missing_c"]
    assert result.merged_length == 2
    assert result.field == "findings"


def test_aggregate_across_states_empty_state_ids_returns_empty_items() -> None:
    """No requested state ids => nothing aggregated and nothing missing."""
    states_outputs = {"s1": {"findings": ["x"]}}
    result = aggregate_across_states(states_outputs, state_ids=[])
    assert result.items == []
    assert result.from_states == []
    assert result.state_count == 0
    assert result.missing_states == []
    assert result.merged_length == 0


def test_aggregate_across_states_present_state_with_missing_field_counts_but_no_items() -> None:
    """A present state whose merge_field is absent/non-list still counts."""
    states_outputs = {
        "s_ok": {"findings": ["ok1"]},
        "s_no_field": {"other": 42},  # merge_field missing entirely
        "s_wrong_type": {"findings": "not-a-list"},
    }
    result = aggregate_across_states(
        states_outputs,
        state_ids=["s_ok", "s_no_field", "s_wrong_type"],
    )
    assert result.items == ["ok1"]
    assert result.state_count == 3
    assert result.missing_states == []
    assert result.merged_length == 1


def test_aggregate_across_states_custom_merge_field_echoed_in_result() -> None:
    """A non-default merge_field is honoured and echoed in ``field``."""
    states_outputs = {
        "s1": {"hits": [10, 20]},
        "s2": {"hits": [30]},
    }
    result = aggregate_across_states(
        states_outputs,
        state_ids=["s1", "s2"],
        merge_field="hits",
    )
    assert result.items == [10, 20, 30]
    assert result.field == "hits"
    assert result.state_count == 2
    assert result.missing_states == []
    assert result.merged_length == 3


def test_aggregate_across_states_is_idempotent() -> None:
    """Calling the helper twice on the same input yields equal results."""
    states_outputs = {
        "a": {"findings": [1, 2]},
        "b": {"findings": [3]},
    }
    first = aggregate_across_states(states_outputs, state_ids=["a", "b", "missing"])
    second = aggregate_across_states(states_outputs, state_ids=["a", "b", "missing"])
    assert first == second
    assert first.items == second.items
    assert first.missing_states == second.missing_states
