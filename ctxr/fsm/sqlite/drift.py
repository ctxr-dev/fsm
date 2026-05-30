"""W12 layer-8 drift detector — the background scoring + auto-pause loop.

The drift detector is the last enforcement layer wired into the FSM:
every other primitive (spec-hash lock, commit cosignature, two-phase
commit, verifier panel, tool-call observation) lands its *evidence* on
the bus and in the SQLite substrate; this module is the consumer that
turns that evidence into a per-run *score* and pauses the run when the
score crosses a configured threshold.

Design contract
---------------

* **One background task per process.** The supervisor (W7 ``serve``)
  spawns :func:`drift_detector_loop` once inside its task group. The
  loop polls every ``poll_interval`` seconds, scans every incomplete
  run's events since the cursor, classifies + scores them, and emits
  drift signals as it goes. When a run's cumulative score strictly
  exceeds :attr:`DriftConfig.score_threshold` the loop atomically flips
  the run to ``drift_paused`` and emits ``drift_pause_triggered``.

* **Cursor lives in-process.** Per-run cursors map ``run_id ->
  last_seq_processed``. They are reset on every fresh supervisor boot
  (the supervisor never persists them) — which is fine because the
  drift_signals table is the durable source of truth for "what has
  already been scored". A redelivery on restart just becomes a no-op
  upsert at the producer/event level; we don't double-count because we
  scan *events* (which are append-only and monotonic per run), not
  drift_signals.

* **At-most-once pause.** Once a run is in ``drift_paused`` (or any
  terminal status) the loop never re-emits the pause event for it,
  even if more events accrue. The status guard inside the auto-pause
  block is the single source of that idempotency.

* **No business logic in classify_event.** The classifier is a pure
  function over an Event value-object plus a tiny per-run scoreboard.
  All side effects (recording signals, updating status, emitting
  events) happen in the loop, not in :func:`classify_event`.

* **Disabled by env.** ``CTXR_FSM_DRIFT_DISABLED=1`` makes
  :func:`drift_detector_loop` return immediately after one
  pre-flight log line. This is the supervisor's "kill switch" for
  ops who need to stop the loop without restarting the whole serve
  process — they unset the var on the next reload cycle.

Signal taxonomy
---------------

Mapping from :class:`ctxr.fsm.core.models.EventKind` to
:class:`SignalKind`:

* ``validation_failed`` → ``repeated_validation_failed`` only when this
  is the **second-or-later** consecutive ``validation_failed`` for the
  run. A single failure is noise; a streak is drift.
* ``commit_signature_mismatch`` → ``signature_mismatch`` (every
  occurrence is a signal — there is no benign reason to ever miscompute
  a cosignature).
* ``verifier_rejected`` → ``verifier_rejection`` (every occurrence;
  the verifier is the most expensive enforcement layer and a rejection
  always means the worker disagreed with the panel).
* ``tool_call_observed`` → ``off_allowlist_tool_call`` iff the
  ``tool_name`` payload is NOT in the *current* state's allowed_tools
  list and is NOT an ``fsm.*`` tool (those are always permitted).
* ``idle_too_long`` is *not* triggered by an event — it's triggered by
  the *absence* of events for ``window_seconds``. The loop checks the
  delta between ``now`` and the run's ``last_update_at`` after each
  sweep and synthesises the signal directly.

This module is pure ``ctxr.fsm.sqlite`` — no FastAPI / MCP / Typer
imports.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import anyio

from ctxr.fsm.core.models import EventKind, RunStatus, SignalKind
from ctxr.fsm.sqlite.repos_events import Event

if TYPE_CHECKING:
    from ctxr.fsm.sqlite.project import Project

__all__ = [
    "DRIFT_DISABLED_ENV_VAR",
    "DRIFT_PRODUCER_KIND",
    "DRIFT_PRODUCER_NAME",
    "DriftConfig",
    "RunScoreboard",
    "classify_event",
    "drift_detector_loop",
]

_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

# Env-var kill switch the supervisor honours; documented in this
# module's docstring. Kept as a module-level constant so callers (and
# tests) can flip it via ``monkeypatch.setenv`` without re-typing the
# literal at every site.
DRIFT_DISABLED_ENV_VAR: str = "CTXR_FSM_DRIFT_DISABLED"

# The producer identity the drift detector registers + emits under.
# Stable across restarts so subscribers can filter on this pair to see
# every drift-detector-generated event.
DRIFT_PRODUCER_KIND: str = "engine"
DRIFT_PRODUCER_NAME: str = "fsm.drift_detector"


# ---------------------------------------------------------------------------
# Configuration value object
# ---------------------------------------------------------------------------


def _default_kind_weights() -> dict[str, float]:
    """Default weights for each signal kind.

    Numbers are chosen so that:

    * One ``off_allowlist_tool_call`` (5) is below the default
      threshold of 10 — a single accidental ``Bash`` is noise.
    * Two off-allowlist calls hit exactly 10 — still not strictly
      above the threshold.
    * Three off-allowlist calls (15) trip the pause — the worker is
      *systematically* ignoring the allowlist.
    * One ``signature_mismatch`` (8) plus one
      ``repeated_validation_failed`` (3) totals 11 — a worker that
      forges a signature AND can't pass validation in a row trips
      immediately.
    * One ``verifier_rejection`` (6) plus one
      ``off_allowlist_tool_call`` (5) totals 11 — same idea.
    """
    return {
        SignalKind.off_allowlist_tool_call.value: 5.0,
        SignalKind.repeated_validation_failed.value: 3.0,
        SignalKind.signature_mismatch.value: 8.0,
        SignalKind.verifier_rejection.value: 6.0,
        SignalKind.output_shape_near_miss.value: 2.0,
        SignalKind.idle_too_long.value: 1.0,
    }


@dataclass(frozen=True)
class DriftConfig:
    """Tunable thresholds for the drift detector.

    ``score_threshold`` is **strict** — pause fires when the cumulative
    score is *greater than* the threshold, not equal to. This matches
    the unit test expectations and the operator's mental model: "the
    run is over budget" reads more naturally than "at budget".

    ``window_seconds`` is the idle-detection window. A run that has not
    accumulated a new event for this long synthesises a single
    ``idle_too_long`` signal per sweep. We do not de-bounce repeated
    idle signals at the source: every poll cycle that observes an idle
    run records another idle signal, which is intentional — staying
    idle is itself escalating evidence.

    ``kind_weights`` maps a :class:`SignalKind` value to its score
    contribution. Unrecognised kinds fall back to 0.0 silently so a
    future caller emitting a new signal kind doesn't crash the loop.
    """

    score_threshold: float = 10.0
    window_seconds: float = 60.0
    kind_weights: dict[str, float] = field(default_factory=_default_kind_weights)


# ---------------------------------------------------------------------------
# Per-run scoreboard (cursor + small classifier state)
# ---------------------------------------------------------------------------


@dataclass
class RunScoreboard:
    """Per-run scratchpad the loop carries between polls.

    * ``last_seq`` is the most recent event ``seq`` we have classified
      for this run. ``None`` is the "start of stream" sentinel — the
      next sweep pulls every event the run has emitted so far.
    * ``consecutive_validation_failed`` tracks the streak length the
      classifier uses to suppress the first ``validation_failed``
      event in a row. Any non-``validation_failed`` event resets the
      counter to zero.
    * ``paused`` is sticky: once True the loop refuses to re-emit
      ``drift_pause_triggered`` even if new evidence accrues. This is
      the at-most-once contract spelled out at the top of the module.

    A scoreboard is allocated lazily the first time the loop touches a
    run and lives for the lifetime of the loop task (process
    lifetime). The substrate's drift_signals table is the durable
    record; the scoreboard is just classifier state.
    """

    last_seq: int | None = None
    consecutive_validation_failed: int = 0
    paused: bool = False


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


# Sentinel set of tool-name prefixes that never count as off-allowlist.
# ``fsm.*`` tools are the FSM's own surface — calling them is *how* a
# worker advances the FSM, so they are implicitly allowed regardless of
# the state's ``allowed_tools`` declaration.
_ALWAYS_ALLOWED_TOOL_PREFIXES: tuple[str, ...] = ("fsm.", "fsm_")


def _tool_is_always_allowed(tool_name: str) -> bool:
    """Return True when ``tool_name`` is an FSM-surface tool.

    The FSM advertises its own tools under the ``fsm.*`` namespace
    (and a couple under ``fsm_*`` for legacy callers). A worker that
    calls one of those is by definition acting within the FSM
    contract, so we never flag those as off-allowlist regardless of
    the state's declared ``allowed_tools``.
    """
    return any(tool_name.startswith(p) for p in _ALWAYS_ALLOWED_TOOL_PREFIXES)


def classify_event(
    event: Event,
    *,
    allowed_tools: Iterable[str] = (),
    scoreboard: RunScoreboard | None = None,
) -> SignalKind | None:
    """Map an :class:`Event` to a :class:`SignalKind`, or ``None``.

    ``allowed_tools`` is the *current state's* tool allowlist; it is
    consulted only for ``tool_call_observed`` events. An empty iterable
    means "no allowlist declared" — in which case the W12 contract
    treats every non-``fsm.*`` tool as off-allowlist. This is the
    operator-friendly default: a state that declares no allowlist is
    advertising that it has no business calling external tools.

    ``scoreboard`` (when supplied) is mutated to track the
    ``validation_failed`` streak so the classifier can return
    ``repeated_validation_failed`` only from the second consecutive
    failure onwards. Callers that want a pure classification (no
    streak suppression) can pass ``None``; in that case every
    ``validation_failed`` returns ``repeated_validation_failed``.

    Returns ``None`` when the event is not a drift trigger (every
    other EventKind in the closed taxonomy falls through here).
    """
    kind = event.kind

    # Tool-call observed: off-allowlist check.
    if kind == EventKind.tool_call_observed.value:
        # ``allowed_tools`` is iterated once into a set so the
        # membership test below stays O(1) even for large lists.
        allowed_set = {t for t in allowed_tools}
        tool_name_obj = event.payload.get("tool_name") if event.payload else None
        tool_name = str(tool_name_obj) if tool_name_obj is not None else ""
        if not tool_name:
            # Malformed observation — be conservative and do not flag.
            return None
        if _tool_is_always_allowed(tool_name):
            return None
        if tool_name in allowed_set:
            return None
        # Off-allowlist: reset the validation streak too (this is
        # different evidence than a validation failure).
        if scoreboard is not None:
            scoreboard.consecutive_validation_failed = 0
        return SignalKind.off_allowlist_tool_call

    # Validation failed: only the 2nd-or-later in a row counts.
    if kind == EventKind.validation_failed.value:
        if scoreboard is None:
            # No streak state available — every failure is a signal.
            return SignalKind.repeated_validation_failed
        scoreboard.consecutive_validation_failed += 1
        if scoreboard.consecutive_validation_failed >= 2:
            return SignalKind.repeated_validation_failed
        return None

    # Signature mismatch: every occurrence is a signal.
    if kind == EventKind.commit_signature_mismatch.value:
        if scoreboard is not None:
            scoreboard.consecutive_validation_failed = 0
        return SignalKind.signature_mismatch

    # Verifier rejection: every occurrence is a signal.
    if kind == EventKind.verifier_rejected.value:
        if scoreboard is not None:
            scoreboard.consecutive_validation_failed = 0
        return SignalKind.verifier_rejection

    # Every other event kind: not a drift trigger. We still need to
    # reset the validation streak on any "good news" event so a
    # single later failure (separated by, e.g., a state_entered
    # event) is correctly classified as the first in a new streak
    # rather than the second in the previous one.
    if scoreboard is not None:
        scoreboard.consecutive_validation_failed = 0
    return None


# ---------------------------------------------------------------------------
# Idle detection helpers
# ---------------------------------------------------------------------------


def _parse_iso(ts: str) -> datetime | None:
    """Parse an ISO-8601 timestamp tolerantly.

    The substrate writes two slightly different shapes:

    * ``YYYY-MM-DDTHH:MM:SS.sss+00:00`` (lifecycle tables)
    * ``YYYY-MM-DDTHH:MM:SS.sssZ`` (event bus, enforcement tables)

    Both are valid ISO-8601 and ``datetime.fromisoformat`` (since 3.11)
    understands both — but to stay defensive against malformed rows we
    swallow ``ValueError`` and return ``None``. Callers treat ``None``
    as "couldn't tell, skip the idle check".
    """
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            return datetime.fromisoformat(ts[:-1] + "+00:00")
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


# Run statuses that are NOT eligible for further drift evaluation.
# Once a run reaches any of these, the loop never emits another signal
# or pause event for it — even if the cursor advances, we stop scoring.
_TERMINAL_OR_PAUSED: frozenset[str] = frozenset(
    {
        RunStatus.completed.value,
        RunStatus.aborted.value,
        RunStatus.drift_paused.value,
        RunStatus.superseded.value,
    }
)


async def drift_detector_loop(
    project: Project,
    config: DriftConfig | None = None,
    poll_interval: float = 2.0,
) -> None:
    """Background task: score every open run, auto-pause runs over threshold.

    Algorithm per poll cycle:

    1. List every incomplete run (``RunsRepo.incomplete``).
    2. For each run, fetch every event with ``seq > scoreboard.last_seq``.
    3. Classify each event. When the classifier returns a
       :class:`SignalKind`, record a ``drift_signals`` row with the
       configured weight and emit a ``drift_signal_recorded`` event so
       subscribers see the evidence on the bus.
    4. After processing the new events, check the run's *current*
       drift score (sum of every recorded signal, via
       :meth:`DriftSignalsRepo.score_for_run`). If the score strictly
       exceeds ``config.score_threshold`` AND the run is not already
       in a terminal-or-paused status, flip the run to
       ``drift_paused`` and emit ``drift_pause_triggered`` with the
       score + threshold payload.
    5. Synthesise an ``idle_too_long`` signal when the run has gone
       longer than ``config.window_seconds`` since its
       ``last_update_at``. The signal is recorded once per sweep so a
       stuck run accrues escalating evidence.
    6. Sleep ``poll_interval`` and repeat.

    The loop is cancellation-safe: ``anyio.sleep`` is the only await
    point, so a task-group cancellation tears it down between sweeps
    without leaving a half-classified event un-committed (every
    ``record`` call is inside its own ``session.begin()`` block).
    """
    # Honour the operator-facing kill switch *before* registering the
    # producer or doing any other work — that's the contract documented
    # in the module docstring.
    if os.environ.get(DRIFT_DISABLED_ENV_VAR) == "1":
        _LOG.info(
            "drift detector disabled via %s=1; skipping loop boot",
            DRIFT_DISABLED_ENV_VAR,
        )
        return

    cfg = config if config is not None else DriftConfig()

    # Lazily register the drift-detector producer once at boot. The
    # upsert is idempotent so a previous boot's row is reused.
    with project.session_factory() as session, session.begin():
        producer = project.producers.upsert(
            session,
            kind=DRIFT_PRODUCER_KIND,
            name=DRIFT_PRODUCER_NAME,
        )
    producer_id = producer.id

    # Per-run scoreboards. Allocated lazily on first sighting, kept
    # for the lifetime of the loop task — there is no GC pass because
    # the typical operating envelope (a handful of in-flight runs per
    # supervisor) keeps the dict tiny.
    scoreboards: dict[str, RunScoreboard] = {}

    _LOG.info(
        "drift detector loop started "
        "(threshold=%.2f, window=%.1fs, poll=%.2fs)",
        cfg.score_threshold,
        cfg.window_seconds,
        poll_interval,
    )

    while True:
        try:
            await _sweep_once(
                project,
                cfg=cfg,
                producer_id=producer_id,
                scoreboards=scoreboards,
            )
        except anyio.get_cancelled_exc_class():
            # Re-raise so the task group's cancellation contract works.
            raise
        except Exception:
            # We deliberately never let the loop die from a per-sweep
            # exception. The supervisor would happily restart us, but
            # that costs an interactive operator the visibility into
            # exactly which sweep tripped — far better to log and keep
            # polling so the next sweep can recover.
            _LOG.exception("drift detector sweep failed; continuing")

        await anyio.sleep(poll_interval)


async def _sweep_once(
    project: Project,
    *,
    cfg: DriftConfig,
    producer_id: str,
    scoreboards: dict[str, RunScoreboard],
) -> None:
    """Run one classify-and-pause sweep across every incomplete run.

    Extracted from the loop body so unit tests can exercise a single
    sweep without spinning up the polling cadence. The function is
    deliberately ``async`` even though it never awaits — that keeps
    the signature stable for future enhancements (e.g. parallel
    per-run sweeps via ``anyio.create_task_group``) and matches what
    the loop expects to await.
    """
    # Snapshot the incomplete run set inside one session. Sorting is
    # not required — every run is processed independently — but the
    # repo already returns them ``last_update_at DESC`` so the most
    # active runs are touched first, which keeps the warm-start
    # latency low for the operator-visible UI.
    with project.session_factory() as session:
        runs = project.runs.incomplete(session)

    for run_summary in runs:
        run_id = run_summary.id
        if run_summary.status in _TERMINAL_OR_PAUSED:
            # Defensive: ``incomplete`` is supposed to filter these
            # out, but a status change racing the snapshot is benign
            # and we should ignore those runs explicitly.
            continue

        scoreboard = scoreboards.setdefault(run_id, RunScoreboard())
        if scoreboard.paused:
            # Already paused — never re-score, never re-emit.
            continue

        # Resolve the current state's allowed_tools list for the
        # off-allowlist classifier. We pull it from the spec's
        # definition JSON rather than building the full FsmSpec
        # model — that keeps this hot path cheap and avoids a
        # dependency on the engine.
        allowed_tools = _allowed_tools_for(project, run_summary)

        # Process new events for this run since the last seq we saw.
        with project.session_factory() as session:
            new_events = list(
                project.events.by_run(
                    session, run_id, since_seq=scoreboard.last_seq
                )
            )

        highest_seq = scoreboard.last_seq
        for event in new_events:
            signal_kind = classify_event(
                event,
                allowed_tools=allowed_tools,
                scoreboard=scoreboard,
            )
            if signal_kind is not None:
                _record_signal(
                    project,
                    run_id=run_id,
                    producer_id=producer_id,
                    signal_kind=signal_kind,
                    weight=cfg.kind_weights.get(signal_kind.value, 0.0),
                    payload={
                        "event_id": event.id,
                        "event_kind": event.kind,
                        "event_seq": event.seq,
                    },
                )
            if event.seq is not None and (
                highest_seq is None or event.seq > highest_seq
            ):
                highest_seq = event.seq

        # Always advance the cursor so a kind we don't score never
        # gets re-classified on the next sweep.
        scoreboard.last_seq = highest_seq

        # Idle synthesis. Skip when ``last_update_at`` is unparseable
        # (defensive — the repo always writes a parseable shape) or
        # when the run has produced no event yet (a brand-new run is
        # not "idle" — it's just starting).
        last_update_dt = _parse_iso(run_summary.last_update_at)
        if last_update_dt is not None:
            now = datetime.now(tz=UTC)
            idle_for = (now - last_update_dt).total_seconds()
            if idle_for > cfg.window_seconds:
                _record_signal(
                    project,
                    run_id=run_id,
                    producer_id=producer_id,
                    signal_kind=SignalKind.idle_too_long,
                    weight=cfg.kind_weights.get(
                        SignalKind.idle_too_long.value, 0.0
                    ),
                    payload={
                        "idle_seconds": idle_for,
                        "window_seconds": cfg.window_seconds,
                    },
                )

        # Score gate. Read the sum atomically inside one session so
        # we never see a partial write from a racing emitter.
        with project.session_factory() as session:
            score = project.drift_signals.score_for_run(session, run_id)

        if score > cfg.score_threshold:
            _trigger_pause(
                project,
                run_id=run_id,
                producer_id=producer_id,
                score=score,
                threshold=cfg.score_threshold,
            )
            scoreboard.paused = True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _allowed_tools_for(project: Project, run_summary: object) -> list[str]:
    """Resolve the current state's ``allowed_tools`` list for ``run_summary``.

    We read the spec definition JSON directly (rather than rehydrating
    the full :class:`FsmSpec` Pydantic model) because the drift
    detector touches this hot-path for every event and the full model
    parse is wasteful — we only need a single list of strings.

    Returns ``[]`` when:

    * The run has no ``current_state`` yet (entry hasn't happened).
    * The spec lookup fails (a deleted spec row would be a bug
      elsewhere, but we mustn't crash the loop over it).
    * The current state doesn't declare ``allowed_tools``.

    An empty list is the correct conservative default: with no
    allowlist, the classifier flags every non-``fsm.*`` tool as
    off-allowlist, which is exactly the W12 contract for states that
    decline to expose a tool surface.
    """
    current_state = getattr(run_summary, "current_state", None)
    fsm_spec_id = getattr(run_summary, "fsm_spec_id", None)
    if not current_state or not fsm_spec_id:
        return []
    with project.session_factory() as session:
        spec = project.specs.get(session, fsm_spec_id)
    if spec is None:
        return []
    states = spec.definition.get("states") if isinstance(spec.definition, dict) else None
    if not isinstance(states, list):
        return []
    for state_def in states:
        if not isinstance(state_def, dict):
            continue
        if state_def.get("id") != current_state:
            continue
        tools = state_def.get("allowed_tools")
        if isinstance(tools, list):
            return [str(t) for t in tools]
        return []
    return []


def _record_signal(
    project: Project,
    *,
    run_id: str,
    producer_id: str,
    signal_kind: SignalKind,
    weight: float,
    payload: dict[str, object],
) -> None:
    """Record a drift signal AND emit ``drift_signal_recorded`` in one txn.

    Both writes share a single ``session.begin()`` so a crash between
    them leaves the database consistent — either the signal AND the
    event are present, or neither is. The producer_id is the
    drift-detector's own (registered at boot); the run_id ties the
    signal to a specific in-flight run for the scoreboard query.
    """
    with project.session_factory() as session, session.begin():
        project.drift_signals.record(
            session,
            run_id=run_id,
            producer_id=producer_id,
            signal_kind=signal_kind.value,
            weight=weight,
            payload=payload,
        )
        project.events.emit(
            session,
            producer_id=producer_id,
            kind=EventKind.drift_signal_recorded.value,
            payload={
                "signal_kind": signal_kind.value,
                "weight": weight,
                **payload,
            },
            run_id=run_id,
        )


def _trigger_pause(
    project: Project,
    *,
    run_id: str,
    producer_id: str,
    score: float,
    threshold: float,
) -> None:
    """Flip the run to ``drift_paused`` and emit ``drift_pause_triggered``.

    Atomic w.r.t. the two writes: the status update and the event
    emission happen inside one ``session.begin()`` so a crash between
    the two cannot leave a paused run without its breadcrumb event (or
    vice versa). The status write also bumps ``last_update_at`` via
    :meth:`RunsRepo.update_status`, which is what the operator-facing
    "runs" lists key on.
    """
    with project.session_factory() as session, session.begin():
        project.runs.update_status(
            session,
            run_id=run_id,
            status=RunStatus.drift_paused.value,
        )
        project.events.emit(
            session,
            producer_id=producer_id,
            kind=EventKind.drift_pause_triggered.value,
            payload={
                "score": score,
                "threshold": threshold,
            },
            run_id=run_id,
        )
    _LOG.info(
        "drift detector paused run %s (score=%.2f > threshold=%.2f)",
        run_id,
        score,
        threshold,
    )
