"""``drift_paused`` auto-clears in commit_outputs when only idle signals fired.

The new policy: idle-only pauses are an artefact of slow worker
dispatches and should not require an operator to call resume_run.
Activity (commit_outputs, get_brief, heartbeat) proves the LLM is
alive and clears the pause in place; a ``drift_pause_cleared`` event
records the auto-clear with the contributing signal kinds.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from ctxr.fsm.core.models import EventKind, RunStatus, SignalKind
from ctxr.fsm.mcp.tools_meta import fsm_heartbeat
from ctxr.fsm.mcp.tools_runs import CommitOutputsInput, fsm_commit_outputs
from ctxr.fsm.sqlite.drift import DriftConfig
from ctxr.fsm.sqlite.models_events import EventTable

from .conftest import backdate_last_update, run_one_sweep, seed_run


def _pause_via_idle_only(project, run_id) -> None:
    """Force ``run_id`` into drift_paused with idle_too_long signals only.

    The idle signal weight is intentionally 1.0 so we need >10 sweeps
    over a short window to cross threshold. We use a tiny window
    (1s) + a backdated last_update_at so each sweep records a fresh
    idle signal until the cumulative score crosses 10.
    """
    backdate_last_update(
        project, run_id=run_id, iso_ts="2020-01-01T00:00:00.000Z"
    )
    cfg = DriftConfig(window_seconds=1.0)
    for _ in range(12):
        run_one_sweep(project, config=cfg)


def test_idle_only_pause_clears_on_commit_outputs(project_factory) -> None:
    """Idle-only drift_paused auto-clears on commit_outputs; commit succeeds.

    The auto-clear emits ``drift_pause_cleared`` listing the
    contributing kinds; the run status flips back to in_progress
    BEFORE the rest of commit_outputs runs so the commit advances
    normally.
    """
    project = project_factory()
    try:
        run_id = seed_run(project)
        _pause_via_idle_only(project, run_id)

        run = project.get_run(run_id)
        assert run is not None
        assert run.status == RunStatus.drift_paused.value

        result = fsm_commit_outputs(
            CommitOutputsInput(
                run_id=uuid.UUID(run_id),
                outputs={"hello": "world"},
            )
        )
        # Either the commit advanced (kind=advanced/loop_continued/terminal)
        # OR it short-circuited for an unrelated reason — but it MUST
        # not be the run_drift_paused refusal envelope.
        if hasattr(result, "error"):
            assert result.error != "run_drift_paused", (
                "idle-only pause should auto-clear before commit gate fires"
            )

        # The auto-clear breadcrumb event lands on the bus.
        with project.session_factory() as session:
            cleared_events = session.execute(
                select(EventTable).where(
                    EventTable.run_id == run_id,
                    EventTable.kind == EventKind.drift_pause_cleared.value,
                )
            ).scalars().all()
        assert len(cleared_events) == 1
    finally:
        project.close()


def test_idle_only_pause_clears_on_heartbeat(project_factory) -> None:
    """Heartbeat also auto-clears idle-only drift_paused.

    The HeartbeatResult.drift_pause_cleared flag is True when the
    auto-clear fired, so an orchestrator can branch on it without a
    second event-bus query.
    """
    project = project_factory()
    try:
        run_id = seed_run(project)
        _pause_via_idle_only(project, run_id)

        run = project.get_run(run_id)
        assert run is not None
        assert run.status == RunStatus.drift_paused.value

        result = fsm_heartbeat(run_id=uuid.UUID(run_id), message="alive")
        assert not hasattr(result, "error"), f"unexpected error: {result!r}"
        assert result.drift_pause_cleared is True

        run_after = project.get_run(run_id)
        assert run_after is not None
        assert run_after.status == RunStatus.in_progress.value
    finally:
        project.close()


def test_drift_pause_cleared_payload_lists_contributing_kinds(
    project_factory,
) -> None:
    """The breadcrumb payload lists every signal kind that supported the pause.

    For an idle-only auto-clear the payload contains exactly the
    ``idle_too_long`` kind (and nothing else).
    """
    project = project_factory()
    try:
        run_id = seed_run(project)
        _pause_via_idle_only(project, run_id)

        result = fsm_heartbeat(run_id=uuid.UUID(run_id), message="")
        assert not hasattr(result, "error")
        assert result.drift_pause_cleared is True

        import json

        with project.session_factory() as session:
            events = session.execute(
                select(EventTable).where(
                    EventTable.run_id == run_id,
                    EventTable.kind == EventKind.drift_pause_cleared.value,
                )
            ).scalars().all()
        assert len(events) == 1
        payload = json.loads(events[0].payload_json)
        assert payload["contributing_signal_kinds"] == [
            SignalKind.idle_too_long.value
        ]
        assert "cleared_by" in payload
        assert "cleared_at" in payload
    finally:
        project.close()
