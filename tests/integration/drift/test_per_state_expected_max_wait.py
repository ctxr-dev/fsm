"""Per-state ``Worker.expected_max_wait_seconds`` overrides the global window.

A slow worker (e.g. a 10-minute codebase scan) can declare an
explicit budget on its Worker spec. The drift detector honours that
budget instead of ``DriftConfig.window_seconds`` for the run's
current state.
"""

from __future__ import annotations

from ctxr.fsm.sqlite.drift import DriftConfig

from .conftest import backdate_last_update, minimal_worker_spec, run_one_sweep, seed_run


def test_per_state_budget_suppresses_idle_signal(project_factory) -> None:
    """A Worker with expected_max_wait_seconds=600 does NOT trip a 60s sweep.

    Sequence:
    1. Seed a run with a Worker declaring ``expected_max_wait_seconds=600``.
    2. Backdate last_update_at to ~400s ago — over the global default
       (300s) but UNDER the per-state budget (600s).
    3. Sweep with a tight global window (60s).
    4. Assert NO idle_too_long signal was recorded, because the
       per-state override wins.
    """
    project = project_factory()
    try:
        spec = minimal_worker_spec(
            spec_id="long_running_demo", expected_max_wait_seconds=600
        )
        run_id = seed_run(project, spec=spec)

        # 400s ago — clearly inside the 600s per-state budget but well
        # past the tight 60s global window the sweep below uses.
        from datetime import UTC, datetime, timedelta

        ts = (datetime.now(tz=UTC) - timedelta(seconds=400)).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")
        backdate_last_update(project, run_id=run_id, iso_ts=ts)

        cfg = DriftConfig(window_seconds=60.0)
        run_one_sweep(project, config=cfg)

        with project.session_factory() as session:
            signals = project.drift_signals.by_run(session, run_id)
        idle_signals = [s for s in signals if s.signal_kind == "idle_too_long"]
        assert idle_signals == [], (
            "per-state expected_max_wait_seconds should suppress idle signal"
        )
    finally:
        project.close()


def test_no_per_state_budget_uses_global_window(project_factory) -> None:
    """A Worker without the override falls back to ``cfg.window_seconds``.

    Same setup as above but the Worker omits ``expected_max_wait_seconds``,
    so the 60s sweep window applies and the 400s-stale run trips the
    idle signal.
    """
    project = project_factory()
    try:
        spec = minimal_worker_spec(spec_id="default_window_demo")
        run_id = seed_run(project, spec=spec)

        from datetime import UTC, datetime, timedelta

        ts = (datetime.now(tz=UTC) - timedelta(seconds=400)).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")
        backdate_last_update(project, run_id=run_id, iso_ts=ts)

        cfg = DriftConfig(window_seconds=60.0)
        run_one_sweep(project, config=cfg)

        with project.session_factory() as session:
            signals = project.drift_signals.by_run(session, run_id)
        idle_signals = [s for s in signals if s.signal_kind == "idle_too_long"]
        assert len(idle_signals) >= 1
    finally:
        project.close()
