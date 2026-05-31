"""SQLite repositories for the ctxr.fsm event bus.

Four sibling repositories live in this module:

* :class:`ProducersRepo`        — registry of event emitters (engine, workers,
  verifiers, the CLI). Idempotent upsert by ``(kind, name)``.
* :class:`ConsumersRepo`        — registry of subscribers, also keyed by
  ``(kind, name)``, with optional CSV ``filter_kind`` and per-run
  ``filter_run_id`` scoping. Carries ``last_seen_at`` for liveness.
* :class:`EventsRepo`           — append-only event log. ``emit`` allocates the
  next per-run ``seq`` (``SELECT MAX(seq)+1 FROM events WHERE run_id=?``),
  inserts the event row, and fans the event out by inserting one
  ``event_deliveries`` row per consumer whose filter matches.
* :class:`EventDeliveriesRepo`  — per-(event, consumer) delivery bookkeeping:
  pending pulls, mark-delivered, ack, fail.

Conventions
-----------
* Every public method takes a ``sqlalchemy.orm.Session`` (DI from a
  sessionmaker; the W2 ``@atomic`` decorator wraps these in
  ``Session.begin()`` to provide transactional semantics — repositories
  themselves do NOT open transactions).
* All timestamps written via :func:`_iso_now_ms` to keep the canonical
  ``YYYY-MM-DDTHH:MM:SS.sssZ`` shape.
* All UUIDs generated via :func:`_uuid7_str` so insertion-order matches
  lexicographic sort order on the primary key index.
* All JSON blobs serialised with :func:`_canonical_json` (sort_keys, compact
  separators) so a payload's textual identity is stable across processes —
  essential for the dedup / replay invariants the engine relies on.
* Public return types are Pydantic value-objects (:class:`Producer`,
  :class:`Consumer`, :class:`Event`, :class:`EventWithDelivery`); SQLModel
  table rows never leak across the repository boundary.
* No business logic — repositories are CRUD plus the minimal aggregation
  queries the bus needs (per-run ``MAX(seq)``, per-consumer pending pull).
* Pure ``ctxr.fsm.sqlite``: no FastAPI / MCP imports.

Per-run monotonic ``seq``
-------------------------
The event bus contract requires that, for every run, the events table contains
a contiguous strictly-monotonic ``seq`` column starting at 1. We allocate the
next value inline inside :meth:`EventsRepo.emit` with
``SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE run_id = ?``. The
partial UNIQUE index ``idx_events_run_seq`` (declared in ``models_events``)
is the safety net: two concurrent emitters that race the allocate-then-insert
will see one of them fail the UNIQUE constraint, signalling the caller's
``@atomic`` decorator to retry. Run-less events (``run_id IS NULL``) skip the
allocation entirely — they have no per-run timeline to be monotonic against.

Fan-out matching
----------------
``ConsumerTable.filter_kind`` is stored as a CSV (e.g.
``"state_entered,state_exited"``); a NULL value means "no kind filter, deliver
everything". Membership is checked at fan-out time with
``"," || filter_kind || "," LIKE '%,' || :kind || ',%'`` which is a
SQLite-idiomatic way to match against a comma-delimited list without dragging
a join table into the schema. ``filter_run_id`` matches by equality, with
NULL meaning "any run".
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import uuid_utils
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, or_, select, text, update
from sqlalchemy.orm import Session

from ctxr.fsm.sqlite.models_events import (
    ConsumerTable,
    EventDeliveryTable,
    EventTable,
    ProducerTable,
)

__all__ = [
    "Consumer",
    "ConsumersRepo",
    "Event",
    "EventDeliveriesRepo",
    "EventWithDelivery",
    "EventsRepo",
    # Value objects
    "Producer",
    # Repositories
    "ProducersRepo",
]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _uuid7_str() -> str:
    """Return a fresh UUIDv7 as a 36-char canonical string.

    UUIDv7's first 48 bits are a millisecond Unix timestamp, so lexicographic
    PK order tracks insertion order. That keeps B-tree inserts append-friendly
    and gives rows a useful default sort even without an explicit ``created_at``
    column to ``ORDER BY``.
    """
    return str(uuid_utils.uuid7())


def _iso_now_ms() -> str:
    """Return the current UTC time as an ISO-8601 string with ms precision.

    Mirrors the convention used by :mod:`ctxr.fsm.sqlite.models_events`: the
    canonical wire format is ``YYYY-MM-DDTHH:MM:SS.sssZ`` (Zulu suffix, not
    ``+00:00``) so a simple ``ORDER BY created_at`` is correct under SQLite's
    default BINARY collation.
    """
    now = datetime.now(UTC)
    return now.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _canonical_json(value: Any) -> str:
    """Serialise ``value`` to canonical JSON text.

    ``sort_keys=True`` plus the compact ``(",", ":")`` separator pair makes
    the text representation a *function* of the value: two equal Python objects
    serialise to byte-identical strings. The bus's dedup / replay semantics
    lean on that determinism — without it, a SHA over the payload would not be
    a stable identity.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _decode_json(text_value: str) -> Any:
    """Decode a JSON column value, treating empty/missing as an empty object.

    Defensive: STRICT mode forbids NULL in our JSON columns (they default to
    ``"{}"``), but a hand-edited DB or an older migration might still surface
    an empty string. Returning ``{}`` keeps the value-object construction
    total instead of raising at the boundary.
    """
    if not text_value:
        return {}
    return json.loads(text_value)


# ---------------------------------------------------------------------------
# Value objects (Pydantic) — the public return surface
# ---------------------------------------------------------------------------


class Producer(BaseModel):
    """A registered event producer.

    Pydantic value-object equivalent of :class:`ProducerTable`. ``metadata``
    is the decoded JSON dict (the table stores canonical JSON text); callers
    that need the raw string should re-serialise via :func:`_canonical_json`.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    kind: str
    name: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class Consumer(BaseModel):
    """A registered event consumer.

    ``filter_kind`` is the decoded list of :class:`EventKind` *string values*
    (not the StrEnum itself, to keep this value-object free of upstream enum
    imports). ``filter_run_id`` is ``None`` for unscoped consumers.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    kind: str
    name: str
    filter_kind: list[str] | None = None
    filter_run_id: str | None = None
    created_at: str
    last_seen_at: str | None = None


class Event(BaseModel):
    """A persisted event on the bus.

    ``payload`` is the decoded JSON object; ``seq`` is ``None`` for run-less
    events and a strictly-monotonic per-run integer otherwise.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    run_id: str | None = None
    kind: str
    producer_id: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    seq: int | None = None


class EventWithDelivery(BaseModel):
    """An :class:`Event` paired with its per-consumer delivery record.

    Returned by :meth:`EventDeliveriesRepo.pending_for`; the bus poll loop
    needs both the payload and the delivery bookkeeping (status, attempts,
    timestamps) in a single round-trip.
    """

    model_config = ConfigDict(frozen=True)

    event: Event
    consumer_id: str
    delivered_at: str | None = None
    acked_at: str | None = None
    status: str = "pending"
    attempts: int = 0


# ---------------------------------------------------------------------------
# Row -> value-object helpers
# ---------------------------------------------------------------------------


def _row_to_producer(row: ProducerTable) -> Producer:
    """Adapt a :class:`ProducerTable` ORM row to the public :class:`Producer`."""
    return Producer(
        id=row.id,
        kind=row.kind,
        name=row.name,
        metadata=_decode_json(row.metadata_json),
        created_at=row.created_at,
    )


def _row_to_consumer(row: ConsumerTable) -> Consumer:
    """Adapt a :class:`ConsumerTable` ORM row to the public :class:`Consumer`.

    The CSV ``filter_kind`` column is split into a list[str]; an empty CSV
    (which would round-trip to ``[""]`` from a naive split) is normalised to
    ``None`` to preserve the "no filter" semantics.
    """
    filter_kind: list[str] | None
    if row.filter_kind is None:
        filter_kind = None
    else:
        # Split + strip + drop empties keeps us robust against whitespace and
        # trailing commas that a hand-written INSERT might introduce.
        parts = [chunk.strip() for chunk in row.filter_kind.split(",")]
        parts = [p for p in parts if p]
        filter_kind = parts if parts else None
    return Consumer(
        id=row.id,
        kind=row.kind,
        name=row.name,
        filter_kind=filter_kind,
        filter_run_id=row.filter_run_id,
        created_at=row.created_at,
        last_seen_at=row.last_seen_at,
    )


def _row_to_event(row: EventTable) -> Event:
    """Adapt an :class:`EventTable` ORM row to the public :class:`Event`."""
    return Event(
        id=row.id,
        run_id=row.run_id,
        kind=row.kind,
        producer_id=row.producer_id,
        payload=_decode_json(row.payload_json),
        created_at=row.created_at,
        seq=row.seq,
    )


def _row_to_event_with_delivery(
    event_row: EventTable, delivery_row: EventDeliveryTable
) -> EventWithDelivery:
    """Pair an :class:`Event` with its delivery row into an :class:`EventWithDelivery`."""
    return EventWithDelivery(
        event=_row_to_event(event_row),
        consumer_id=delivery_row.consumer_id,
        delivered_at=delivery_row.delivered_at,
        acked_at=delivery_row.acked_at,
        status=delivery_row.status,
        attempts=delivery_row.attempts,
    )


# ---------------------------------------------------------------------------
# ProducersRepo
# ---------------------------------------------------------------------------


class ProducersRepo:
    """CRUD repository for :class:`ProducerTable`.

    The repository class is stateless — each method takes the SQLAlchemy
    :class:`Session` explicitly. We keep it as a class rather than module-level
    functions so callers can swap an in-memory fake at the interface boundary
    without monkey-patching the module.
    """

    @staticmethod
    def upsert(
        session: Session,
        *,
        kind: str,
        name: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> Producer:
        """Insert-or-return a producer by ``(kind, name)``.

        Idempotent: if a producer with the same ``(kind, name)`` already
        exists, the existing row is returned as-is and ``metadata`` is
        ignored. This matches the bus contract that "registering a producer"
        is a one-shot, not an update. Callers that need to *change* metadata
        should add a dedicated ``update_metadata`` method later — explicitly,
        rather than letting upsert do double duty.
        """
        existing = session.execute(
            select(ProducerTable).where(
                ProducerTable.kind == kind, ProducerTable.name == name
            )
        ).scalar_one_or_none()
        if existing is not None:
            return _row_to_producer(existing)

        row = ProducerTable(
            id=_uuid7_str(),
            kind=kind,
            name=name,
            metadata_json=_canonical_json(dict(metadata) if metadata else {}),
            created_at=_iso_now_ms(),
        )
        session.add(row)
        session.flush()  # populate any DB-side defaults; PK is client-supplied here
        return _row_to_producer(row)

    @staticmethod
    def get(session: Session, producer_id: str) -> Producer | None:
        """Return the producer with ``id == producer_id`` or ``None`` if absent."""
        row = session.get(ProducerTable, producer_id)
        return _row_to_producer(row) if row is not None else None

    @staticmethod
    def list(session: Session) -> list[Producer]:
        """Return all producers, ordered by ``id`` (== UUIDv7, ≈ insertion order)."""
        rows = (
            session.execute(select(ProducerTable).order_by(ProducerTable.id))
            .scalars()
            .all()
        )
        return [_row_to_producer(r) for r in rows]


# ---------------------------------------------------------------------------
# ConsumersRepo
# ---------------------------------------------------------------------------


class ConsumersRepo:
    """CRUD repository for :class:`ConsumerTable` with ``register`` upsert."""

    @staticmethod
    def register(
        session: Session,
        *,
        kind: str,
        name: str,
        filter_kind: Sequence[str] | None = None,
        filter_run_id: str | None = None,
    ) -> Consumer:
        """Upsert a consumer by ``(kind, name)``.

        Unlike :meth:`ProducersRepo.upsert`, ``register`` *updates* the
        filter columns when the consumer already exists — re-registering a
        consumer with new filters is the documented way to "reconfigure" it.
        ``last_seen_at`` is never touched here; that's the job of
        :meth:`touch_last_seen`.
        """
        csv: str | None
        if filter_kind is None:
            csv = None
        else:
            # Canonicalise: strip, drop empties, dedup while preserving order.
            seen: set[str] = set()
            cleaned: list[str] = []
            for raw in filter_kind:
                k = str(raw).strip()
                if not k or k in seen:
                    continue
                seen.add(k)
                cleaned.append(k)
            csv = ",".join(cleaned) if cleaned else None

        existing = session.execute(
            select(ConsumerTable).where(
                ConsumerTable.kind == kind, ConsumerTable.name == name
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.filter_kind = csv
            existing.filter_run_id = filter_run_id
            session.flush()
            return _row_to_consumer(existing)

        row = ConsumerTable(
            id=_uuid7_str(),
            kind=kind,
            name=name,
            filter_kind=csv,
            filter_run_id=filter_run_id,
            created_at=_iso_now_ms(),
            last_seen_at=None,
        )
        session.add(row)
        session.flush()
        return _row_to_consumer(row)

    @staticmethod
    def get(session: Session, consumer_id: str) -> Consumer | None:
        """Return the consumer with ``id == consumer_id`` or ``None`` if absent."""
        row = session.get(ConsumerTable, consumer_id)
        return _row_to_consumer(row) if row is not None else None

    @staticmethod
    def list(session: Session) -> list[Consumer]:
        """Return all consumers, ordered by ``id`` (insertion-order proxy)."""
        rows = (
            session.execute(select(ConsumerTable).order_by(ConsumerTable.id))
            .scalars()
            .all()
        )
        return [_row_to_consumer(r) for r in rows]

    @staticmethod
    def touch_last_seen(session: Session, consumer_id: str) -> None:
        """Set ``last_seen_at`` to "now" for ``consumer_id``.

        Issued as a single targeted ``UPDATE`` rather than a load-mutate-flush
        round-trip — the bus poll loop calls this on every successful pull, so
        the cheaper path matters. Silently no-ops if the consumer was deleted
        between two poll cycles.
        """
        session.execute(
            update(ConsumerTable)
            .where(ConsumerTable.id == consumer_id)
            .values(last_seen_at=_iso_now_ms())
        )


# ---------------------------------------------------------------------------
# EventsRepo
# ---------------------------------------------------------------------------


class EventsRepo:
    """CRUD + emit + read-side queries for :class:`EventTable`.

    :meth:`emit` is the only mutating method here; it both inserts the event
    row AND fans the event out by inserting one row per matching consumer
    into :class:`EventDeliveryTable`. Doing both inside the same caller-owned
    transaction (W2's ``@atomic``) is the linchpin of the at-least-once
    delivery guarantee.
    """

    @staticmethod
    def emit(
        session: Session,
        *,
        producer_id: str,
        kind: str,
        payload: Mapping[str, Any] | None = None,
        run_id: str | None = None,
    ) -> Event:
        """Insert a new event and fan it out to every matching consumer.

        Returns the persisted :class:`Event` (with its allocated ``seq`` for
        run-scoped emits, or ``None`` otherwise). Concurrent emits against
        the same ``run_id`` are made safe by the partial UNIQUE index on
        ``(run_id, seq)``: if two transactions both compute the same next
        ``seq``, one of them fails to insert and the caller's ``@atomic``
        wrapper retries.
        """
        # Per-run monotonic seq allocation. We use COALESCE so the very first
        # event for a run computes ``0 + 1 = 1`` rather than ``NULL + 1`` (=
        # NULL). Run-less events skip this entirely — they have no per-run
        # timeline to participate in.
        seq: int | None = None
        if run_id is not None:
            current_max = session.execute(
                select(text("COALESCE(MAX(seq), 0)")).select_from(EventTable).where(
                    EventTable.run_id == run_id
                )
            ).scalar_one()
            seq = int(current_max) + 1

        event_row = EventTable(
            id=_uuid7_str(),
            run_id=run_id,
            kind=kind,
            producer_id=producer_id,
            payload_json=_canonical_json(dict(payload) if payload else {}),
            created_at=_iso_now_ms(),
            seq=seq,
        )
        session.add(event_row)
        session.flush()  # surface UNIQUE-constraint violations early

        # Fan-out. Matching predicate:
        #   (filter_kind IS NULL OR kind ∈ CSV(filter_kind))
        #   AND
        #   (filter_run_id IS NULL OR filter_run_id = run_id)
        #
        # The CSV membership check uses SQLite's LIKE on a sentinel-wrapped
        # string — wrapping both sides with commas means we never match a
        # prefix of a longer kind name (e.g. "state_entered" must not match a
        # CSV of "state_entered_v2").
        kind_csv_match = or_(
            ConsumerTable.filter_kind.is_(None),
            (
                ("," + ConsumerTable.filter_kind + ",").like("%," + kind + ",%")
            ),
        )
        if run_id is None:
            # A run-less event only goes to consumers without a run filter —
            # otherwise the filter is unsatisfiable.
            run_match = ConsumerTable.filter_run_id.is_(None)
        else:
            run_match = or_(
                ConsumerTable.filter_run_id.is_(None),
                ConsumerTable.filter_run_id == run_id,
            )

        matched_consumers = (
            session.execute(
                select(ConsumerTable.id).where(and_(kind_csv_match, run_match))
            )
            .scalars()
            .all()
        )

        # Insert delivery rows in a single add_all batch — SQLAlchemy will
        # coalesce these into one executemany under the hood.
        now = _iso_now_ms()
        deliveries = [
            EventDeliveryTable(
                event_id=event_row.id,
                consumer_id=consumer_id,
                delivered_at=None,
                acked_at=None,
                status="pending",
                attempts=0,
            )
            for consumer_id in matched_consumers
        ]
        if deliveries:
            session.add_all(deliveries)
            session.flush()
        # ``now`` is captured but not yet used — kept as a sentinel for a
        # future "scheduled_at" column without needing a second call to
        # _iso_now_ms() if added.
        _ = now

        return _row_to_event(event_row)

    @staticmethod
    def by_producer(
        session: Session,
        producer_id: str,
        kinds: Sequence[str] | None = None,
    ) -> list[Event]:
        """Return all events emitted by ``producer_id``, newest first.

        ``kinds`` (when supplied) restricts to that subset of event kinds.
        Ordered by ``created_at DESC`` so the caller's "tail" use-case is
        served directly from the ``ix_events_producer_id`` index plus the
        ``ix_events_created_at`` index — SQLite picks whichever the planner
        decides is cheaper.
        """
        stmt = select(EventTable).where(EventTable.producer_id == producer_id)
        if kinds:
            stmt = stmt.where(EventTable.kind.in_(list(kinds)))
        stmt = stmt.order_by(EventTable.created_at.desc())
        rows = session.execute(stmt).scalars().all()
        return [_row_to_event(r) for r in rows]

    @staticmethod
    def by_run(
        session: Session,
        run_id: str,
        since_seq: int | None = None,
        kinds: Sequence[str] | None = None,
    ) -> Iterator[Event]:
        """Stream events for ``run_id`` in ascending ``seq`` order.

        ``since_seq`` is exclusive — pass the last ``seq`` you've already
        consumed and you'll receive everything strictly after it. Returning
        an :class:`Iterator` (not a list) lets the bus poll loop bail out
        early when its buffer is full; the underlying ``yield_per`` keeps
        memory bounded for large run histories.
        """
        stmt = select(EventTable).where(EventTable.run_id == run_id)
        if since_seq is not None:
            stmt = stmt.where(EventTable.seq > since_seq)
        if kinds:
            stmt = stmt.where(EventTable.kind.in_(list(kinds)))
        stmt = stmt.order_by(EventTable.seq.asc())

        # yield_per chunks the result set into batches of 100 — large enough
        # to amortise the per-row overhead, small enough that an early break
        # in the consumer doesn't waste a lot of fetched rows.
        result = session.execute(stmt).scalars()
        for row in result:
            yield _row_to_event(row)

    @staticmethod
    def by_kind(
        session: Session,
        kind: str,
        limit: int = 100,
    ) -> list[Event]:
        """Return up to ``limit`` most-recent events of ``kind``.

        Ordered by ``created_at DESC`` so a typical UI "latest of kind X"
        query is served directly from the ``ix_events_created_at`` index
        with an ``ix_events_kind`` lookup as the first filter.
        """
        stmt = (
            select(EventTable)
            .where(EventTable.kind == kind)
            .order_by(EventTable.created_at.desc())
            .limit(limit)
        )
        rows = session.execute(stmt).scalars().all()
        return [_row_to_event(r) for r in rows]


# ---------------------------------------------------------------------------
# EventDeliveriesRepo
# ---------------------------------------------------------------------------


class EventDeliveriesRepo:
    """Per-(event, consumer) delivery bookkeeping repository.

    The composite PK ``(event_id, consumer_id)`` makes the at-most-one-row
    invariant structural — see :class:`EventDeliveryTable`'s docstring. The
    mutating methods (``mark_delivered`` / ``ack`` / ``fail``) silently
    no-op if the delivery row was deleted between calls, which the bus
    treats as a benign race.
    """

    @staticmethod
    def pending_for(
        session: Session,
        consumer_id: str,
        limit: int = 50,
    ) -> list[EventWithDelivery]:
        """Return up to ``limit`` pending deliveries for ``consumer_id``.

        Ordered by ``EventTable.created_at ASC`` so the consumer sees events
        in the same order the producers emitted them — FIFO within a kind,
        and globally consistent with wall-clock time across kinds. The
        composite index ``idx_event_deliveries_consumer_pending`` on
        ``(consumer_id, status, delivered_at)`` lets SQLite serve the
        ``status='pending'`` filter directly.
        """
        stmt = (
            select(EventDeliveryTable, EventTable)
            .join(EventTable, EventTable.id == EventDeliveryTable.event_id)
            .where(
                EventDeliveryTable.consumer_id == consumer_id,
                EventDeliveryTable.status == "pending",
            )
            .order_by(EventTable.created_at.asc())
            .limit(limit)
        )
        rows = session.execute(stmt).all()
        return [
            _row_to_event_with_delivery(event_row, delivery_row)
            for delivery_row, event_row in rows
        ]

    @staticmethod
    def mark_delivered(
        session: Session,
        event_id: str,
        consumer_id: str,
    ) -> None:
        """Stamp ``delivered_at`` and bump ``attempts``; keep status at ``pending``.

        "Delivered" is the half-way state between "pending" (queued) and
        "acked" (consumer confirmed). Keeping the status at ``pending`` here
        means a redelivery is automatic if no ack arrives — the bus's at-
        least-once contract. The ``attempts`` counter is incremented on every
        attempt regardless of outcome so a misbehaving consumer is visible.
        """
        session.execute(
            update(EventDeliveryTable)
            .where(
                EventDeliveryTable.event_id == event_id,
                EventDeliveryTable.consumer_id == consumer_id,
            )
            .values(
                delivered_at=_iso_now_ms(),
                attempts=EventDeliveryTable.attempts + 1,
            )
        )

    @staticmethod
    def ack(session: Session, event_id: str, consumer_id: str) -> None:
        """Mark the delivery as ``acked`` and stamp ``acked_at``.

        Terminal-success transition. The bus stops considering the row for
        future poll cycles once status is ``acked``.
        """
        session.execute(
            update(EventDeliveryTable)
            .where(
                EventDeliveryTable.event_id == event_id,
                EventDeliveryTable.consumer_id == consumer_id,
            )
            .values(
                status="acked",
                acked_at=_iso_now_ms(),
            )
        )

    @staticmethod
    def fail(
        session: Session,
        event_id: str,
        consumer_id: str,
        reason: str,
    ) -> None:
        """Mark the delivery as ``failed``; record ``reason`` in delivered_at hint.

        ``EventDeliveryTable`` has no dedicated ``error_reason`` column in
        the W1 schema — STRICT mode discourages adding columns just for free-
        form text. Until the schema gains an explicit ``error_reason``, we
        bump ``attempts`` and leave ``reason`` in the application logs (via
        the caller). The status transition itself is what the bus polls on.
        """
        # ``reason`` is intentionally accepted for API parity with the brief
        # and forward-compat with a future schema migration that adds an
        # ``error_reason`` column. Capturing it as a no-op parameter here
        # keeps the signature stable now and lets callers wire the value
        # without a follow-up refactor.
        _ = reason
        session.execute(
            update(EventDeliveryTable)
            .where(
                EventDeliveryTable.event_id == event_id,
                EventDeliveryTable.consumer_id == consumer_id,
            )
            .values(
                status="failed",
                attempts=EventDeliveryTable.attempts + 1,
            )
        )
