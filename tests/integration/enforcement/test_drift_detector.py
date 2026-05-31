"""Integration tests for the W12 layer-8 drift detector.

These tests synthesise event streams against a real SQLite database
and exercise the drift detector's classifier + scoring + auto-pause
flow end-to-end. The detector is driven by calling its sweep
primitive directly (``_sweep_once``) rather than spinning the polling
loop — that gives us deterministic, single-step assertions without
sleeping the test suite waiting for the next poll cycle.

Scenarios covered
-----------------

* ``test_below_threshold_no_pause`` — one ``off_allowlist_tool_call``
  (weight 5) sits at half the default threshold (10); the loop
  records the signal but never pauses the run.
* ``test_at_threshold_no_pause`` — two off-allowlist calls hit the
  threshold exactly (weight 10); the pause gate is strictly
  greater-than, so we still expect no pause.
* ``test_above_threshold_pauses_run`` — three off-allowlist calls
  push the score to 15 > 10; the run flips to ``drift_paused`` and
  emits ``drift_pause_triggered`` exactly once.
* ``test_pause_is_idempotent_across_sweeps`` — after the first pause,
  re-running the sweep does not emit a second pause event or accrue
  more signals (a sticky-paused scoreboard is the contract).
* ``test_signature_mismatch_alone_triggers_pause`` — a single
  ``commit_signature_mismatch`` (weight 8) plus a single
  ``verifier_rejected`` (weight 6) totals 14, well above threshold.
* ``test_classifier_suppresses_first_validation_failure`` — direct
  classifier-level assertion that the first ``validation_failed``
  in a row returns ``None`` and the second returns
  ``repeated_validation_failed``.
* ``test_drift_disabled_env_var_short_circuits_loop`` — flipping
  ``CTXR_FSM_DRIFT_DISABLED=1`` makes the loop entry function return
  immediately without binding the producer or scoring anything.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import anyio
import pytest

from ctxr.fsm.core.models import (
    EventKind,
    FsmSpec,
    RunStatus,
    SignalKind,
    State,
    Transition,
)
from ctxr.fsm.sqlite import Project
from ctxr.fsm.sqlite.drift import (
    DRIFT_DISABLED_ENV_VAR,
    DRIFT_PRODUCER_KIND,
    DRIFT_PRODUCER_NAME,
    DriftConfig,
    RunScoreboard,
    _sweep_once,
    classify_event,
    drift_detector_loop,
)
from ctxr.fsm.sqlite.repos_events import Event

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def project_factory():
    """Open a fresh Project per test on a temp SQLite database.

    The factory pattern lets a test open the project, mutate it, and
    optionally close + reopen against the same DB to simulate a
    supervisor restart. Each invocation returns a live Project that
    the caller is responsible for closing in a ``try/finally``.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fsm.sqlite3"

        def _open() -> Project:
            return Project.open(db_path, migrate=True)

        yield _open


def _minimal_spec(spec_id: str = "drift_demo") -> FsmSpec:
    """A two-state spec used by the drift tests.

    The entry state ``a`` declares ``allowed_tools=["Read"]`` so the
    classifier has a non-empty allowlist to bounce ``Bash`` off of.
    The terminal state ``b`` exists to keep the spec structurally
    valid; the tests never advance the run to it.
    """
    return FsmSpec(
        id=spec_id,
        version=1,
        entry="a",
        states=[
            State(
                id="a",
                purpose="entry with limited tool surface",
                allowed_tools=["Read"],
                transitions=[Transition(to="b", when="always")],
            ),
            State(id="b", purpose="terminal", transitions=[]),
        ],
    )


def _seed_run(project: Project) -> str:
    """Register the spec, start a run, mark current_state, return run id.

    We poke the run row's ``current_state`` field directly because
    bare ``Project.start_run`` deliberately does not transition the
    engine — the W2 facade only inserts the run row. The drift
    detector consults ``current_state`` to look up the active
    state's ``allowed_tools``, so the tests must set it explicitly.
    """
    registered = project.register_spec(_minimal_spec())
    run = project.start_run(registered.spec.id, args={})
    # Poke current_state so the allowed_tools lookup works.
    from ctxr.fsm.sqlite.models_core import RunTable

    with project.session_factory() as session, session.begin():
        row = session.get(RunTable, run.id)
        assert row is not None
        row.current_state = "a"
        session.add(row)
    return run.id


def _emit_tool_call_observed(
    project: Project,
    *,
    run_id: str,
    tool_name: str,
) -> None:
    """Emit a ``tool_call_observed`` event with the given ``tool_name``.

    Uses the same producer the real MCP ``fsm.observe_tool_call`` tool
    uses (kind=``agent``, name=``test-agent``) so the producer table
    stays consistent across test runs. The payload mirrors the shape
    the MCP tool emits — the classifier reads ``payload['tool_name']``.
    """
    with project.session_factory() as session, session.begin():
        producer = project.producers.upsert(
            session,
            kind="agent",
            name="test-agent",
        )
        project.events.emit(
            session,
            producer_id=producer.id,
            kind=EventKind.tool_call_observed.value,
            payload={
                "tool_name": tool_name,
                "succeeded": True,
                "args_redacted": {},
            },
            run_id=run_id,
        )


def _emit_simple(
    project: Project,
    *,
    run_id: str,
    kind: EventKind,
    payload: dict | None = None,
) -> None:
    """Emit an event of arbitrary kind for ``run_id``."""
    with project.session_factory() as session, session.begin():
        producer = project.producers.upsert(
            session,
            kind="engine",
            name="test-engine",
        )
        project.events.emit(
            session,
            producer_id=producer.id,
            kind=kind.value,
            payload=payload or {},
            run_id=run_id,
        )


def _run_one_sweep(project: Project, *, config: DriftConfig | None = None) -> None:
    """Drive a single drift-detector sweep against ``project``.

    The sweep needs the drift producer registered first (the loop
    does that at boot); we mirror that boot step here so the sweep's
    ``producer_id`` argument is real. Calling ``anyio.run`` per
    sweep is cheap (no event loop reuse across calls) and keeps the
    tests synchronous from pytest's perspective.
    """
    cfg = config if config is not None else DriftConfig()
    scoreboards: dict[str, RunScoreboard] = {}

    with project.session_factory() as session, session.begin():
        producer = project.producers.upsert(
            session,
            kind=DRIFT_PRODUCER_KIND,
            name=DRIFT_PRODUCER_NAME,
        )
    producer_id = producer.id

    async def _go() -> None:
        await _sweep_once(
            project,
            cfg=cfg,
            producer_id=producer_id,
            scoreboards=scoreboards,
        )

    anyio.run(_go)


def _run_n_sweeps(
    project: Project,
    n: int,
    *,
    config: DriftConfig | None = None,
) -> dict[str, RunScoreboard]:
    """Drive ``n`` sweeps with a shared scoreboard, return final scoreboards.

    Tests that need to verify idempotency-across-sweeps must keep the
    scoreboard between iterations — that's the same lifetime the
    real loop gives it. Returns the scoreboard map so tests can
    assert on internal state if needed.
    """
    cfg = config if config is not None else DriftConfig()
    scoreboards: dict[str, RunScoreboard] = {}

    with project.session_factory() as session, session.begin():
        producer = project.producers.upsert(
            session,
            kind=DRIFT_PRODUCER_KIND,
            name=DRIFT_PRODUCER_NAME,
        )
    producer_id = producer.id

    async def _go() -> None:
        for _ in range(n):
            await _sweep_once(
                project,
                cfg=cfg,
                producer_id=producer_id,
                scoreboards=scoreboards,
            )

    anyio.run(_go)
    return scoreboards


# ---------------------------------------------------------------------------
# End-to-end: score accumulation + auto-pause
# ---------------------------------------------------------------------------


def test_below_threshold_no_pause(project_factory) -> None:
    """One off-allowlist call (weight 5) is below the threshold (10).

    Flow: register spec, start run, emit ONE ``Bash`` tool call,
    sweep. Expect exactly one ``drift_signals`` row, no pause event,
    run status untouched.
    """
    # Use a long idle window so the synthetic "idle" signal does
    # not muddle the test — every run we just started is by
    # definition "idle" against a 1s window.
    cfg = DriftConfig(window_seconds=3600.0)
    project = project_factory()
    try:
        run_id = _seed_run(project)
        _emit_tool_call_observed(project, run_id=run_id, tool_name="Bash")

        _run_one_sweep(project, config=cfg)

        # Exactly one drift signal recorded with the expected weight.
        with project.session_factory() as session:
            signals = project.drift_signals.by_run(session, run_id)
            score = project.drift_signals.score_for_run(session, run_id)
        assert len(signals) == 1
        assert signals[0].signal_kind == SignalKind.off_allowlist_tool_call.value
        assert signals[0].weight == 5.0
        assert score == 5.0

        # Run status unchanged — pause threshold not crossed.
        run = project.get_run(run_id)
        assert run is not None
        assert run.status == RunStatus.in_progress.value

        # No drift_pause_triggered event on the bus.
        from sqlalchemy import select

        from ctxr.fsm.sqlite.models_events import EventTable

        with project.session_factory() as session:
            pause_events = session.execute(
                select(EventTable).where(
                    EventTable.run_id == run_id,
                    EventTable.kind == EventKind.drift_pause_triggered.value,
                )
            ).scalars().all()
        assert pause_events == []
    finally:
        project.close()


def test_at_threshold_no_pause(project_factory) -> None:
    """Two off-allowlist calls (5 + 5 = 10) sit AT the threshold.

    The pause gate is strictly greater-than (``score > threshold``),
    so the run must remain in_progress. This is the canonical
    "edge case that documents the operator's mental model" — at
    budget is not over budget.
    """
    cfg = DriftConfig(window_seconds=3600.0)
    project = project_factory()
    try:
        run_id = _seed_run(project)
        _emit_tool_call_observed(project, run_id=run_id, tool_name="Bash")
        _emit_tool_call_observed(project, run_id=run_id, tool_name="WebFetch")

        _run_one_sweep(project, config=cfg)

        with project.session_factory() as session:
            score = project.drift_signals.score_for_run(session, run_id)
        assert score == 10.0

        run = project.get_run(run_id)
        assert run is not None
        assert run.status == RunStatus.in_progress.value, (
            "the threshold is strict — at-budget must not pause"
        )
    finally:
        project.close()


def test_above_threshold_pauses_run(project_factory) -> None:
    """Three off-allowlist calls (5 + 5 + 5 = 15 > 10) trip the pause.

    Asserts:

    * Each call recorded a ``drift_signal_recorded`` event.
    * The cumulative score crossed the threshold.
    * The run status flipped to ``drift_paused``.
    * Exactly one ``drift_pause_triggered`` event was emitted, with
      the ``score`` and ``threshold`` payload populated.
    """
    cfg = DriftConfig(window_seconds=3600.0)
    project = project_factory()
    try:
        run_id = _seed_run(project)
        _emit_tool_call_observed(project, run_id=run_id, tool_name="Bash")
        _emit_tool_call_observed(project, run_id=run_id, tool_name="WebFetch")
        _emit_tool_call_observed(project, run_id=run_id, tool_name="Edit")

        _run_one_sweep(project, config=cfg)

        # Three drift_signals rows + cumulative score above threshold.
        with project.session_factory() as session:
            signals = project.drift_signals.by_run(session, run_id)
            score = project.drift_signals.score_for_run(session, run_id)
        assert len(signals) == 3
        assert all(
            s.signal_kind == SignalKind.off_allowlist_tool_call.value for s in signals
        )
        assert score == 15.0

        # Run status flipped to drift_paused.
        run = project.get_run(run_id)
        assert run is not None
        assert run.status == RunStatus.drift_paused.value

        # The pause event landed on the bus exactly once with the
        # correct score + threshold payload.
        from sqlalchemy import select

        from ctxr.fsm.sqlite.models_events import EventTable

        with project.session_factory() as session:
            pause_events = session.execute(
                select(EventTable).where(
                    EventTable.run_id == run_id,
                    EventTable.kind == EventKind.drift_pause_triggered.value,
                )
            ).scalars().all()
        assert len(pause_events) == 1
        import json

        payload = json.loads(pause_events[0].payload_json)
        assert payload["score"] == 15.0
        assert payload["threshold"] == 10.0

        # And one drift_signal_recorded event per signal.
        with project.session_factory() as session:
            signal_events = session.execute(
                select(EventTable).where(
                    EventTable.run_id == run_id,
                    EventTable.kind == EventKind.drift_signal_recorded.value,
                )
            ).scalars().all()
        assert len(signal_events) == 3
    finally:
        project.close()


def test_pause_is_idempotent_across_sweeps(project_factory) -> None:
    """Once paused, subsequent sweeps must not re-emit the pause event.

    Pause is sticky on the scoreboard side: once a run is flagged
    ``paused=True``, the loop never re-scores it or re-emits the
    pause event. We emit three off-allowlist calls, sweep three
    times, and assert that exactly one pause event accumulated.
    """
    cfg = DriftConfig(window_seconds=3600.0)
    project = project_factory()
    try:
        run_id = _seed_run(project)
        _emit_tool_call_observed(project, run_id=run_id, tool_name="Bash")
        _emit_tool_call_observed(project, run_id=run_id, tool_name="WebFetch")
        _emit_tool_call_observed(project, run_id=run_id, tool_name="Edit")

        scoreboards = _run_n_sweeps(project, 3, config=cfg)

        # The scoreboard for this run is paused.
        assert scoreboards[run_id].paused is True

        # Exactly one pause event despite three sweeps.
        from sqlalchemy import select

        from ctxr.fsm.sqlite.models_events import EventTable

        with project.session_factory() as session:
            pause_events = session.execute(
                select(EventTable).where(
                    EventTable.run_id == run_id,
                    EventTable.kind == EventKind.drift_pause_triggered.value,
                )
            ).scalars().all()
        assert len(pause_events) == 1
    finally:
        project.close()


def test_signature_mismatch_plus_verifier_rejection_pauses(project_factory) -> None:
    """``signature_mismatch`` (8) + ``verifier_rejection`` (6) = 14 > 10.

    Confirms the cross-kind scoring works — the threshold is on the
    sum across signal kinds, not per-kind.
    """
    cfg = DriftConfig(window_seconds=3600.0)
    project = project_factory()
    try:
        run_id = _seed_run(project)
        _emit_simple(
            project,
            run_id=run_id,
            kind=EventKind.commit_signature_mismatch,
        )
        _emit_simple(
            project,
            run_id=run_id,
            kind=EventKind.verifier_rejected,
        )

        _run_one_sweep(project, config=cfg)

        with project.session_factory() as session:
            score = project.drift_signals.score_for_run(session, run_id)
        assert score == 14.0

        run = project.get_run(run_id)
        assert run is not None
        assert run.status == RunStatus.drift_paused.value
    finally:
        project.close()


# ---------------------------------------------------------------------------
# Classifier-level unit tests
# ---------------------------------------------------------------------------


def _make_event(kind: EventKind, *, payload: dict | None = None, seq: int = 1) -> Event:
    """Build a synthetic :class:`Event` value-object for classifier tests.

    The classifier never inspects the producer/run/timestamp fields,
    so we leave them as placeholders. The payload (when relevant) is
    what makes the assertions meaningful.
    """
    return Event(
        id="00000000-0000-0000-0000-000000000000",
        run_id="00000000-0000-0000-0000-000000000001",
        kind=kind.value,
        producer_id="00000000-0000-0000-0000-000000000002",
        payload=payload or {},
        created_at="2026-01-01T00:00:00.000Z",
        seq=seq,
    )


def test_classifier_suppresses_first_validation_failure() -> None:
    """First validation_failed → None; second in a row → repeated_validation_failed."""
    scoreboard = RunScoreboard()

    first = classify_event(_make_event(EventKind.validation_failed), scoreboard=scoreboard)
    assert first is None

    second = classify_event(_make_event(EventKind.validation_failed), scoreboard=scoreboard)
    assert second == SignalKind.repeated_validation_failed

    third = classify_event(_make_event(EventKind.validation_failed), scoreboard=scoreboard)
    assert third == SignalKind.repeated_validation_failed


def test_classifier_resets_validation_streak_on_unrelated_event() -> None:
    """A non-failure event resets the streak so the next failure starts over.

    Sequence: validation_failed (None), state_entered (no signal, resets),
    validation_failed (None again — first in a fresh streak),
    validation_failed (now the 2nd, returns the signal).
    """
    scoreboard = RunScoreboard()
    classify_event(_make_event(EventKind.validation_failed), scoreboard=scoreboard)
    classify_event(_make_event(EventKind.state_entered), scoreboard=scoreboard)
    assert scoreboard.consecutive_validation_failed == 0

    assert (
        classify_event(_make_event(EventKind.validation_failed), scoreboard=scoreboard)
        is None
    )
    assert (
        classify_event(_make_event(EventKind.validation_failed), scoreboard=scoreboard)
        == SignalKind.repeated_validation_failed
    )


def test_classifier_off_allowlist_respects_allowed_tools() -> None:
    """A tool in the allowlist must NOT be flagged off-allowlist."""
    event = _make_event(
        EventKind.tool_call_observed, payload={"tool_name": "Read"}
    )
    assert classify_event(event, allowed_tools=["Read", "Edit"]) is None

    bash = _make_event(
        EventKind.tool_call_observed, payload={"tool_name": "Bash"}
    )
    assert (
        classify_event(bash, allowed_tools=["Read", "Edit"])
        == SignalKind.off_allowlist_tool_call
    )


def test_classifier_fsm_tools_are_always_allowed() -> None:
    """``fsm.*`` tools are implicitly allowed regardless of the allowlist."""
    fsm_get_brief = _make_event(
        EventKind.tool_call_observed, payload={"tool_name": "fsm.get_brief"}
    )
    # Empty allowlist would normally flag every non-fsm tool — but
    # fsm.* is always allowed.
    assert classify_event(fsm_get_brief, allowed_tools=[]) is None


# ---------------------------------------------------------------------------
# Kill-switch + loop entry
# ---------------------------------------------------------------------------


def test_drift_disabled_env_var_short_circuits_loop(
    project_factory, monkeypatch
) -> None:
    """``CTXR_FSM_DRIFT_DISABLED=1`` makes the loop return without scoring.

    Smoke test for the ops kill switch. We set the env var, then
    run the loop with a 0-poll-interval; the function must return
    immediately (no producer registered, no signal recorded) even
    though we emit a clearly-drifty event.
    """
    monkeypatch.setenv(DRIFT_DISABLED_ENV_VAR, "1")
    project = project_factory()
    try:
        run_id = _seed_run(project)
        # Emit enough off-allowlist calls to trip the threshold if
        # the loop were running.
        for tool in ("Bash", "WebFetch", "Edit"):
            _emit_tool_call_observed(project, run_id=run_id, tool_name=tool)

        async def _go() -> None:
            # ``move_on_after`` lives inside the event loop so the
            # cancellation contract is honoured even if (counter to
            # the disable contract) the loop would otherwise spin.
            with anyio.move_on_after(2.0):
                await drift_detector_loop(
                    project,
                    DriftConfig(window_seconds=3600.0),
                    poll_interval=0.01,
                )

        # The function must return cleanly without spinning the loop.
        anyio.run(_go)

        # No drift_signals recorded, no pause.
        with project.session_factory() as session:
            signals = project.drift_signals.by_run(session, run_id)
        assert signals == []

        run = project.get_run(run_id)
        assert run is not None
        assert run.status == RunStatus.in_progress.value
    finally:
        project.close()
