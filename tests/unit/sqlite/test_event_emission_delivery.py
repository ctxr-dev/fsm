"""Unit tests for the SQLite event-bus repositories.

Covers the public surface of :mod:`ctxr.fsm.sqlite.repos_events` via the
:class:`Project` facade — only public imports from
``ctxr.fsm.sqlite`` are used so the tests double as a smoke-check of the
package's re-exports.

Each test runs against a fresh on-disk SQLite database under a per-test
``tempfile.TemporaryDirectory()`` to guarantee isolation: no shared
schema state, no leftover rows, no cross-test ordering surprises.

Coverage:

* ``ProducersRepo.upsert`` is idempotent by ``(kind, name)``.
* ``ConsumersRepo.register`` with a CSV ``filter_kind`` only delivers
  events whose ``kind`` is in that set.
* ``EventsRepo.emit`` fans an event out to every matching consumer.
* ``EventDeliveriesRepo.ack`` flips delivery status to ``acked``.
* ``EventDeliveriesRepo.pending_for`` honours the ``limit`` argument.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from ctxr.fsm.sqlite import Project

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def project() -> Iterator[Project]:
    """Yield a fresh :class:`Project` bound to a temporary SQLite database.

    Each test gets its own ``TemporaryDirectory`` so the underlying DB file
    is destroyed as soon as the fixture tears down, guaranteeing no
    cross-test contamination.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "fsm.sqlite"
        proj = Project.open(db_path)
        try:
            yield proj
        finally:
            proj.close()


# ---------------------------------------------------------------------------
# ProducersRepo.upsert
# ---------------------------------------------------------------------------


def test_producers_upsert_is_idempotent_by_kind_and_name(project: Project) -> None:
    """A second upsert with the same ``(kind, name)`` returns the same row."""
    with project.session_factory() as session, session.begin():
        first = project.producers.upsert(
            session, kind="engine", name="fsm.runtime", metadata={"version": "1"}
        )
        second = project.producers.upsert(
            session, kind="engine", name="fsm.runtime", metadata={"version": "2"}
        )

    assert first.id == second.id
    # Metadata from the first upsert wins — second call is a no-op on metadata.
    assert first.metadata == {"version": "1"}
    assert second.metadata == {"version": "1"}

    # And the registry should contain exactly one producer with that key.
    with project.session_factory() as session:
        listed = project.producers.list(session)
    matching = [p for p in listed if p.kind == "engine" and p.name == "fsm.runtime"]
    assert len(matching) == 1


def test_producers_upsert_distinguishes_kind_and_name(project: Project) -> None:
    """Different ``(kind, name)`` tuples produce distinct producers."""
    with project.session_factory() as session, session.begin():
        a = project.producers.upsert(session, kind="engine", name="alpha")
        b = project.producers.upsert(session, kind="engine", name="beta")
        c = project.producers.upsert(session, kind="worker", name="alpha")

    assert len({a.id, b.id, c.id}) == 3


# ---------------------------------------------------------------------------
# ConsumersRepo.register filter_kind semantics
# ---------------------------------------------------------------------------


def test_consumer_with_filter_kind_only_sees_matching_events(
    project: Project,
) -> None:
    """A consumer scoped to ``state_entered,state_exited`` ignores other kinds."""
    with project.session_factory() as session, session.begin():
        producer = project.producers.upsert(session, kind="engine", name="emitter")
        consumer = project.consumers.register(
            session,
            kind="subscriber",
            name="state-watcher",
            filter_kind=["state_entered", "state_exited"],
        )
        # Emit a mix of matching and non-matching kinds.
        project.events.emit(
            session, producer_id=producer.id, kind="state_entered", payload={"n": 1}
        )
        project.events.emit(
            session, producer_id=producer.id, kind="run_started", payload={"n": 2}
        )
        project.events.emit(
            session, producer_id=producer.id, kind="state_exited", payload={"n": 3}
        )
        project.events.emit(
            session, producer_id=producer.id, kind="worker_dispatched", payload={"n": 4}
        )

    with project.session_factory() as session:
        pending = project.event_deliveries.pending_for(session, consumer.id)

    delivered_kinds = sorted(ewd.event.kind for ewd in pending)
    assert delivered_kinds == ["state_entered", "state_exited"]
    # And the filter is faithfully reflected on the value object.
    assert consumer.filter_kind == ["state_entered", "state_exited"]


def test_consumer_without_filter_kind_sees_every_event(project: Project) -> None:
    """A consumer registered with ``filter_kind=None`` receives all kinds."""
    with project.session_factory() as session, session.begin():
        producer = project.producers.upsert(session, kind="engine", name="emitter")
        consumer = project.consumers.register(
            session,
            kind="subscriber",
            name="firehose",
            filter_kind=None,
        )
        for kind in ("state_entered", "run_started", "worker_dispatched"):
            project.events.emit(session, producer_id=producer.id, kind=kind)

    with project.session_factory() as session:
        pending = project.event_deliveries.pending_for(session, consumer.id)

    assert len(pending) == 3
    assert consumer.filter_kind is None


# ---------------------------------------------------------------------------
# EventsRepo.emit fan-out
# ---------------------------------------------------------------------------


def test_emit_fans_out_to_every_matching_consumer(project: Project) -> None:
    """One emit -> one delivery row per matching consumer; none for non-matchers."""
    with project.session_factory() as session, session.begin():
        producer = project.producers.upsert(session, kind="engine", name="emitter")
        # Matching consumers — both subscribe to state_entered.
        entered_a = project.consumers.register(
            session,
            kind="subscriber",
            name="a",
            filter_kind=["state_entered"],
        )
        entered_b = project.consumers.register(
            session,
            kind="subscriber",
            name="b",
            filter_kind=["state_entered", "state_exited"],
        )
        # Non-matching consumer — only listens for worker_dispatched.
        worker_only = project.consumers.register(
            session,
            kind="subscriber",
            name="worker",
            filter_kind=["worker_dispatched"],
        )
        event = project.events.emit(
            session,
            producer_id=producer.id,
            kind="state_entered",
            payload={"state": "draft"},
        )

    assert event.kind == "state_entered"
    assert event.payload == {"state": "draft"}

    with project.session_factory() as session:
        pending_a = project.event_deliveries.pending_for(session, entered_a.id)
        pending_b = project.event_deliveries.pending_for(session, entered_b.id)
        pending_worker = project.event_deliveries.pending_for(session, worker_only.id)

    assert len(pending_a) == 1
    assert pending_a[0].event.id == event.id
    assert len(pending_b) == 1
    assert pending_b[0].event.id == event.id
    assert pending_worker == []


# ---------------------------------------------------------------------------
# EventDeliveriesRepo.ack
# ---------------------------------------------------------------------------


def test_ack_flips_delivery_status_to_acked(project: Project) -> None:
    """``ack`` updates status to ``acked`` and stamps ``acked_at``."""
    with project.session_factory() as session, session.begin():
        producer = project.producers.upsert(session, kind="engine", name="emitter")
        consumer = project.consumers.register(
            session,
            kind="subscriber",
            name="acker",
            filter_kind=None,
        )
        event = project.events.emit(
            session, producer_id=producer.id, kind="state_entered"
        )

    # The fresh delivery starts as pending.
    with project.session_factory() as session:
        pending_before = project.event_deliveries.pending_for(session, consumer.id)
    assert len(pending_before) == 1
    assert pending_before[0].status == "pending"
    assert pending_before[0].acked_at is None

    # Ack it.
    with project.session_factory() as session, session.begin():
        project.event_deliveries.ack(
            session, event_id=event.id, consumer_id=consumer.id
        )

    # Now ``pending_for`` filters it out — only pending rows are returned.
    with project.session_factory() as session:
        pending_after = project.event_deliveries.pending_for(session, consumer.id)
    assert pending_after == []


# ---------------------------------------------------------------------------
# EventDeliveriesRepo.pending_for limit
# ---------------------------------------------------------------------------


def test_pending_for_respects_limit(project: Project) -> None:
    """``pending_for(limit=N)`` returns at most ``N`` rows."""
    total = 10
    with project.session_factory() as session, session.begin():
        producer = project.producers.upsert(session, kind="engine", name="emitter")
        consumer = project.consumers.register(
            session,
            kind="subscriber",
            name="bulk-watcher",
            filter_kind=None,
        )
        for i in range(total):
            project.events.emit(
                session,
                producer_id=producer.id,
                kind="state_entered",
                payload={"i": i},
            )

    # Limit smaller than total — exactly limit rows returned.
    with project.session_factory() as session:
        first_batch = project.event_deliveries.pending_for(
            session, consumer.id, limit=3
        )
    assert len(first_batch) == 3

    # Limit larger than total — all rows returned.
    with project.session_factory() as session:
        full_batch = project.event_deliveries.pending_for(
            session, consumer.id, limit=total + 5
        )
    assert len(full_batch) == total
