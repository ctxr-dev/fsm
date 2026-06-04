"""``fsm.heartbeat`` refreshes the idle window without committing outputs.

The orchestrator calls this on a timer during long worker dispatches
so the drift detector treats the run as alive even when no state has
advanced. Idempotent; auto-clears idle-only drift_paused as a bonus.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from ctxr.fsm.core.models import EventKind
from ctxr.fsm.mcp.tools_meta import fsm_heartbeat
from ctxr.fsm.sqlite.drift import DriftConfig
from ctxr.fsm.sqlite.models_events import EventTable

from .conftest import backdate_last_update, run_one_sweep, seed_run


def test_heartbeat_emits_heartbeat_event(project_factory) -> None:
    """A single heartbeat call emits one ``heartbeat`` event with the message."""
    project = project_factory()
    try:
        run_id = seed_run(project)

        result = fsm_heartbeat(
            run_id=uuid.UUID(run_id), message="still scanning the codebase"
        )
        assert not hasattr(result, "error"), f"unexpected error: {result!r}"
        assert result.ok is True
        assert result.drift_pause_cleared is False

        with project.session_factory() as session:
            events = session.execute(
                select(EventTable).where(
                    EventTable.run_id == run_id,
                    EventTable.kind == EventKind.heartbeat.value,
                )
            ).scalars().all()
        assert len(events) == 1
    finally:
        project.close()


def test_heartbeat_refreshes_drift_window(project_factory) -> None:
    """After heartbeat, a subsequent sweep does not record another idle signal.

    Same shape as the worker_dispatched test, but the orchestrator
    here calls fsm.heartbeat explicitly rather than relying on
    get_brief side effects.
    """
    project = project_factory()
    try:
        run_id = seed_run(project)
        backdate_last_update(
            project, run_id=run_id, iso_ts="2020-01-01T00:00:00.000Z"
        )

        cfg = DriftConfig(window_seconds=60.0)
        scoreboards = run_one_sweep(project, config=cfg)

        with project.session_factory() as session:
            idle_before = sum(
                1
                for s in project.drift_signals.by_run(session, run_id)
                if s.signal_kind == "idle_too_long"
            )
        assert idle_before >= 1

        result = fsm_heartbeat(run_id=uuid.UUID(run_id), message="alive")
        assert not hasattr(result, "error"), f"unexpected error: {result!r}"

        run_one_sweep(project, config=cfg, scoreboards=scoreboards)

        with project.session_factory() as session:
            idle_after = sum(
                1
                for s in project.drift_signals.by_run(session, run_id)
                if s.signal_kind == "idle_too_long"
            )
        assert idle_after == idle_before, (
            "no new idle_too_long signal should accrue after heartbeat"
        )
    finally:
        project.close()


def test_heartbeat_run_not_found(project_factory) -> None:
    """Unknown run id returns a typed ``run_not_found`` error envelope."""
    project = project_factory()
    try:
        random_id = uuid.uuid4()
        result = fsm_heartbeat(run_id=random_id, message="")
        assert hasattr(result, "error"), f"expected error envelope, got {result!r}"
        assert result.error == "run_not_found"
    finally:
        project.close()
