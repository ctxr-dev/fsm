"""Drive an FSM run start-to-completion via the Project facade.

The Python-direct equivalent of an LLM-as-orchestrator loop, used by
the fsm package's own E2E tests so they can seed a populated UI
without spinning up Claude Code. Inline states advance server-side
via the W17a ``Project.commit_and_advance`` facade; worker states get
their outputs looked up from a caller-supplied mapping keyed by
state-id.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ctxr.fsm.sqlite.project import Project

__all__ = ["DriverResult", "drive_run_to_completion"]


@dataclass(frozen=True)
class DriverResult:
    """Summary of a driver-completed run."""

    run_id: str
    status: str
    verdict: str | None
    visited_worker_states: list[str]
    chain_length_total: int


def drive_run_to_completion(
    project: Project,
    *,
    spec_id: str,
    entry_state_id: str,
    args: dict[str, Any],
    worker_outputs: dict[str, dict[str, Any]],
    max_iterations: int = 200,
) -> DriverResult:
    """Run ``spec_id`` start-to-completion, committing canned outputs.

    Each iteration:
      1. Reads ``run.current_state`` (falling back to ``entry_state_id``
         on the very first iteration, when ``Project.start_run`` has
         left ``current_state=None`` for MCP-layer compatibility).
      2. Looks up the worker output to commit from ``worker_outputs``.
         A missing key raises ``KeyError`` — the test author wants to
         know which state the driver hit and didn't have an output for.
      3. Calls ``project.commit_and_advance(run_id, outputs)``. The
         facade walks any following inline-state chain server-side, so
         deterministic states never appear in ``visited_worker_states``.

    Parameters
    ----------
    project:
        An opened :class:`Project` instance.
    spec_id:
        The slug of the registered spec to run (e.g. ``"skill-code-review"``).
    entry_state_id:
        Required because ``Project.start_run`` doesn't yet eagerly
        populate ``runs.current_state`` (the MCP layer historically
        owned that step). The driver uses this to know which state's
        output to commit on the first iteration.
    args:
        The run's input args dict.
    worker_outputs:
        A mapping of ``state_id -> canned worker output``. Inline
        states do not appear here; they're handled server-side.
    max_iterations:
        Safety cap against an infinite loop. Set generously; the
        consumer is expected to know their spec's bounded length.

    Returns
    -------
    DriverResult
        Final run summary. Raises if the run faults or exceeds the
        iteration cap.
    """
    project_repo = project
    with project_repo.session_factory() as session:
        spec_row = project_repo.specs.get_latest_by_slug(session, spec_id)
    if spec_row is None:
        raise ValueError(f"spec {spec_id!r} is not registered in the project DB")

    run = project_repo.start_run(spec_id=spec_row.id, args=args)
    visited: list[str] = []
    chain_total = 0

    for _ in range(max_iterations):
        run_now = project_repo.get_run(run.id)
        if run_now is None:
            raise RuntimeError(f"run {run.id!r} disappeared mid-drive")
        current_state_id = run_now.current_state or entry_state_id
        if current_state_id is None:
            break
        visited.append(current_state_id)

        if current_state_id not in worker_outputs:
            raise KeyError(
                f"driver reached worker state {current_state_id!r} but "
                f"no canned output was supplied. Worker outputs supplied "
                f"for: {sorted(worker_outputs)!r}"
            )

        result = project_repo.commit_and_advance(
            run_id=run.id, outputs=worker_outputs[current_state_id]
        )
        chain_total += int(result.get("chain_length") or 0)
        if result.get("result_kind") == "fault":
            raise RuntimeError(
                f"engine faulted at state {current_state_id!r}: "
                f"{result.get('fault_reason')!r}"
            )
        if result.get("result_kind") == "terminal":
            break
    else:  # for/else: ran out of iterations without breaking
        raise RuntimeError(
            f"driver did not reach a terminal state within "
            f"{max_iterations} iterations (visited: {visited!r})"
        )

    final_run = project_repo.get_run(run.id)
    assert final_run is not None
    return DriverResult(
        run_id=run.id,
        status=final_run.status,
        verdict=final_run.verdict,
        visited_worker_states=visited,
        chain_length_total=chain_total,
    )
