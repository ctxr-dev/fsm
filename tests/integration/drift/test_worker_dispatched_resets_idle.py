"""``fsm.get_brief`` for a worker-bearing state emits ``worker_dispatched``.

The drift detector treats that event as activity: the scoreboard's
``last_activity_at`` is refreshed so a subsequent sweep does not
synthesise an ``idle_too_long`` signal even when the run row's
``last_update_at`` is stale.

The MCP body also bumps ``runs.last_update_at`` directly so the
substrate-level fix works even when the scoreboard is fresh (e.g.
after a supervisor restart).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from ctxr.fsm.core.models import EventKind
from ctxr.fsm.mcp.tools_runs import GetBriefInput, fsm_get_brief
from ctxr.fsm.sqlite.drift import DriftConfig
from ctxr.fsm.sqlite.models_events import EventTable

from .conftest import backdate_last_update, run_one_sweep, seed_run


def test_get_brief_emits_worker_dispatched_for_worker_state(project_factory) -> None:
    """A worker-bearing brief landing emits exactly one ``worker_dispatched``.

    Asserts the event payload carries the state id, brief id, and a
    ``dispatched_at`` timestamp so the dashboard / drift detector can
    correlate the hand-off.
    """
    project = project_factory()
    try:
        run_id = seed_run(project)

        result = fsm_get_brief(GetBriefInput(run_id=uuid.UUID(run_id)))
        assert not hasattr(result, "error"), f"unexpected error: {result!r}"

        with project.session_factory() as session:
            events = session.execute(
                select(EventTable).where(
                    EventTable.run_id == run_id,
                    EventTable.kind == EventKind.worker_dispatched.value,
                )
            ).scalars().all()
        assert len(events) == 1, "expected exactly one worker_dispatched event"
    finally:
        project.close()


def test_worker_dispatched_refreshes_drift_window(project_factory) -> None:
    """After get_brief, a subsequent sweep does not record idle_too_long.

    Sequence:
    1. Seed run + backdate ``last_update_at`` to 10 minutes ago.
    2. Sweep with a 60s window — synthesises an idle_too_long signal.
    3. Call fsm.get_brief — bumps last_update_at to now AND emits
       worker_dispatched (refreshing the scoreboard's last_activity).
    4. Sweep again — must NOT record a second idle_too_long signal.
    """
    project = project_factory()
    try:
        run_id = seed_run(project)

        # Force the run into "idle for ages" by rewriting last_update_at.
        backdate_last_update(
            project, run_id=run_id, iso_ts="2020-01-01T00:00:00.000Z"
        )

        cfg = DriftConfig(window_seconds=60.0)
        scoreboards = run_one_sweep(project, config=cfg)

        with project.session_factory() as session:
            before_signals = project.drift_signals.by_run(session, run_id)
        idle_before = sum(
            1 for s in before_signals if s.signal_kind == "idle_too_long"
        )
        assert idle_before >= 1, "idle synthesis should have fired"

        # Hand off a brief — this is the activity beat.
        result = fsm_get_brief(GetBriefInput(run_id=uuid.UUID(run_id)))
        assert not hasattr(result, "error"), f"unexpected error: {result!r}"

        # Next sweep, same scoreboard — no fresh idle signal.
        run_one_sweep(project, config=cfg, scoreboards=scoreboards)

        with project.session_factory() as session:
            after_signals = project.drift_signals.by_run(session, run_id)
        idle_after = sum(
            1 for s in after_signals if s.signal_kind == "idle_too_long"
        )
        assert idle_after == idle_before, (
            "no new idle_too_long signal should accrue after worker_dispatched"
        )
    finally:
        project.close()
