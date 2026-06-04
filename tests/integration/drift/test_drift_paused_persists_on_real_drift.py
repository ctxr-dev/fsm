"""A drift_paused run with REAL drift signals stays paused.

Auto-clear only fires when the contributing signals were all
``idle_too_long``. Anything else (off-allowlist tool calls, signature
mismatches, verifier rejections) keeps the run paused and forces the
operator to call ``fsm.resume_run`` to acknowledge the drift.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from ctxr.fsm.core.models import EventKind, RunStatus
from ctxr.fsm.mcp.tools_meta import fsm_heartbeat
from ctxr.fsm.mcp.tools_runs import CommitOutputsInput, fsm_commit_outputs
from ctxr.fsm.sqlite.models_events import EventTable

from .conftest import emit_tool_call, run_one_sweep, seed_run


def _pause_via_off_allowlist(project, run_id) -> None:
    """Force ``run_id`` into drift_paused with off-allowlist tool calls.

    Three Bash calls (5 each) sums to 15 > 10 so the next sweep
    flips the run to drift_paused. The current state's worker has
    no allowed_tools declared so every non-fsm.* tool is off-list.
    """
    for tool in ("Bash", "WebFetch", "Edit"):
        emit_tool_call(project, run_id=run_id, tool_name=tool)
    run_one_sweep(project)


def test_off_allowlist_pause_does_not_clear_on_commit(project_factory) -> None:
    """commit_outputs refuses with run_drift_paused and the run STAYS paused."""
    project = project_factory()
    try:
        run_id = seed_run(project)
        _pause_via_off_allowlist(project, run_id)

        run = project.get_run(run_id)
        assert run is not None
        assert run.status == RunStatus.drift_paused.value

        result = fsm_commit_outputs(
            CommitOutputsInput(
                run_id=uuid.UUID(run_id),
                outputs={"hello": "world"},
            )
        )
        assert hasattr(result, "error")
        assert result.error == "run_drift_paused"

        # No drift_pause_cleared event was emitted.
        with project.session_factory() as session:
            cleared_events = session.execute(
                select(EventTable).where(
                    EventTable.run_id == run_id,
                    EventTable.kind == EventKind.drift_pause_cleared.value,
                )
            ).scalars().all()
        assert cleared_events == []

        run_after = project.get_run(run_id)
        assert run_after is not None
        assert run_after.status == RunStatus.drift_paused.value
    finally:
        project.close()


def test_off_allowlist_pause_does_not_clear_on_heartbeat(
    project_factory,
) -> None:
    """heartbeat does NOT auto-clear a real-drift pause."""
    project = project_factory()
    try:
        run_id = seed_run(project)
        _pause_via_off_allowlist(project, run_id)

        result = fsm_heartbeat(run_id=uuid.UUID(run_id), message="")
        assert not hasattr(result, "error")
        assert result.drift_pause_cleared is False

        run_after = project.get_run(run_id)
        assert run_after is not None
        assert run_after.status == RunStatus.drift_paused.value
    finally:
        project.close()
