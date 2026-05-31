"""SQLModel table definitions for the event bus.

This module declares the four tables that back the ``ctxr.fsm`` event bus:

* :class:`ProducerTable`        — registered event producers (engine, workers, …)
* :class:`ConsumerTable`        — registered event consumers (aggregators, MCP subs, …)
* :class:`EventTable`           — the append-only event log
* :class:`EventDeliveryTable`   — per-(event, consumer) delivery bookkeeping

Conventions enforced
--------------------
* Every table is intended to be created with SQLite's STRICT modifier. SQLModel
  0.0.21 does not pass ``sqlite_strict`` through to the DDL compiler natively,
  so we tag each ``__table_args__`` with ``{'info': {'STRICT': True}}``. The
  Alembic env (W2 / later) inspects this hint and appends ``STRICT`` to the
  generated ``CREATE TABLE``. The hint is also surfaced by
  :func:`ctxr.fsm.sqlite.connection.ensure_strict_tables` for diagnostics.
* All primary keys are UUIDv7 strings (TEXT(36)) generated via ``uuid_utils.uuid7``.
  We store them as ``str`` so the on-disk representation stays human-readable
  and stable across drivers; SQLite has no native UUID type anyway.
* All timestamps are stored as ISO-8601 UTC strings with millisecond precision
  via :func:`_iso_now_ms`. Datetime arithmetic happens in Python; the database
  only sorts strings, which works correctly because ISO-8601 is lex-sortable.
* JSON-bearing columns are typed ``str`` and default to ``"{}"``; callers
  serialise via :func:`json.dumps` before insert. Using ``str`` (rather than
  ``sqlalchemy.JSON``) keeps the column type unambiguous under STRICT mode.
* Foreign keys to ``runs.id`` are declared as string FKs so this module does
  not need to import the ``RunTable`` from ``models_core``: SQLAlchemy
  resolves the reference at metadata-finalisation time. ``ondelete='CASCADE'``
  matches the spec; SQLite enforces it only when ``foreign_keys=ON``, which
  ``ctxr.fsm.sqlite.connection`` guarantees on every connection.

This file is pure ``ctxr.fsm.sqlite`` — it imports no FastAPI / MCP code.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import uuid_utils
from sqlalchemy import (
    Column,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlmodel import Field, SQLModel

__all__ = [
    "ConsumerTable",
    "EventDeliveryTable",
    "EventTable",
    "ProducerTable",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _uuid7_str() -> str:
    """Return a fresh UUIDv7 as a 36-char canonical string.

    UUIDv7 is time-ordered: the first 48 bits encode the Unix epoch in
    milliseconds, which makes B-tree index inserts append-friendly and gives
    rows a useful default sort order even without a separate ``created_at``
    column. Stored as TEXT(36) — see module docstring for the rationale.
    """
    return str(uuid_utils.uuid7())


def _iso_now_ms() -> str:
    """Return the current UTC time as an ISO-8601 string with millisecond precision.

    SQLite has no native timestamp type. Using a stringly-typed ISO-8601 column
    keeps ordering correct (ISO-8601 is lex-sortable in UTC) and avoids the
    subtle integer-vs-real ambiguity of Julian-day storage. The ``Z`` suffix
    makes the timezone explicit so downstream tooling never has to guess.
    """
    now = datetime.now(UTC)
    # ``isoformat(timespec='milliseconds')`` yields ``"+00:00"`` for the offset;
    # we rewrite that to a trailing ``Z`` for the canonical Zulu form expected
    # by the rest of the codebase.
    return now.isoformat(timespec="milliseconds").replace("+00:00", "Z")


# A reusable info-dict hint for the Alembic env to translate into STRICT DDL.
# Centralising it in one constant avoids per-table typos and lets a future
# refactor toggle STRICT globally from a single site.
_STRICT_INFO: dict[str, Any] = {"STRICT": True}


# ---------------------------------------------------------------------------
# ProducerTable
# ---------------------------------------------------------------------------


class ProducerTable(SQLModel, table=True):
    """Registered producer of events on the bus.

    A producer is any subsystem that may emit events: the FSM engine itself,
    individual workers, a verifier, the CLI, etc. Producers are identified by
    a ``(kind, name)`` pair — ``kind`` groups producers by role
    (e.g. ``"engine"``, ``"worker"``, ``"verifier"``) and ``name`` is the
    unique handle within that role. The UNIQUE constraint protects against
    accidental double-registration.
    """

    __tablename__ = "producers"
    __table_args__ = (
        UniqueConstraint("kind", "name", name="uq_producers_kind_name"),
        {"sqlite_with_rowid": True, "info": _STRICT_INFO},
    )

    id: str = Field(
        default_factory=_uuid7_str,
        sa_column=Column("id", String(36), primary_key=True, nullable=False),
    )
    kind: str = Field(sa_column=Column("kind", String, nullable=False))
    name: str = Field(sa_column=Column("name", String, nullable=False))
    # JSON payload as a string — see module docstring for why we avoid the
    # sqlalchemy.JSON dialect type here. Callers serialise with json.dumps.
    metadata_json: str = Field(
        default="{}",
        sa_column=Column("metadata_json", String, nullable=False, default="{}"),
    )
    created_at: str = Field(
        default_factory=_iso_now_ms,
        sa_column=Column("created_at", String, nullable=False),
    )


# ---------------------------------------------------------------------------
# ConsumerTable
# ---------------------------------------------------------------------------


class ConsumerTable(SQLModel, table=True):
    """Registered consumer / subscriber to the event bus.

    Consumers are also identified by ``(kind, name)``. A consumer may optionally
    restrict the events it receives:

    * ``filter_kind`` is a CSV of :class:`~ctxr.fsm.core.models.EventKind`
      values. CSV — rather than a join table — is deliberate: the set is small
      (at most ~30 enum values), it is read together every time, and SQLite's
      ``instr()`` / ``LIKE`` are perfectly adequate for the membership test.
    * ``filter_run_id`` scopes the consumer to a single run; ``NULL`` means
      "all runs". The FK to ``runs.id`` enforces referential integrity, with
      ``ON DELETE CASCADE`` so consumers don't outlive their run.

    ``last_seen_at`` is updated by the bus on every successful delivery; it is
    nullable because a freshly registered consumer has yet to see anything.
    """

    __tablename__ = "consumers"
    __table_args__ = (
        UniqueConstraint("kind", "name", name="uq_consumers_kind_name"),
        {"sqlite_with_rowid": True, "info": _STRICT_INFO},
    )

    id: str = Field(
        default_factory=_uuid7_str,
        sa_column=Column("id", String(36), primary_key=True, nullable=False),
    )
    kind: str = Field(sa_column=Column("kind", String, nullable=False))
    name: str = Field(sa_column=Column("name", String, nullable=False))
    # CSV of EventKind values, e.g. "state_entered,state_exited". NULL means
    # "no kind filter — deliver everything". A literal empty string would be
    # ambiguous (no kinds vs. all kinds), so we forbid it by convention at the
    # repository layer rather than via a CHECK constraint.
    filter_kind: str | None = Field(
        default=None,
        sa_column=Column("filter_kind", String, nullable=True),
    )
    filter_run_id: str | None = Field(
        default=None,
        sa_column=Column(
            "filter_run_id",
            String(36),
            ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    created_at: str = Field(
        default_factory=_iso_now_ms,
        sa_column=Column("created_at", String, nullable=False),
    )
    last_seen_at: str | None = Field(
        default=None,
        sa_column=Column("last_seen_at", String, nullable=True),
    )


# ---------------------------------------------------------------------------
# EventTable
# ---------------------------------------------------------------------------


class EventTable(SQLModel, table=True):
    """Append-only log of every event observed by the bus.

    Indices
    -------
    * ``ix_events_run_id``    — fast lookup of all events for a given run.
    * ``ix_events_kind``      — supports kind-filtered listings.
    * ``ix_events_producer_id`` — supports producer-filtered listings.
    * ``ix_events_created_at`` — supports the common "last N events" tail; the
      DESC order is encoded so the planner can satisfy ``ORDER BY created_at
      DESC LIMIT n`` directly from the index without a sort pass.
    * ``idx_events_run_seq``  — UNIQUE per-(run_id, seq), partial WHERE
      run_id IS NOT NULL. This is the core invariant of the per-run monotonic
      sequence: two events can never share (run, seq) within the same run, but
      run-less events (run_id IS NULL) are exempt because ``seq`` is nullable
      for them — they live on a global timeline keyed by ``created_at`` alone.
    """

    __tablename__ = "events"
    __table_args__ = (
        Index(
            "idx_events_run_seq",
            "run_id",
            "seq",
            unique=True,
            # Partial index: only rows that belong to a run participate in the
            # per-run monotonic uniqueness check. ``text(...)`` is used rather
            # than a bound Column reference so the predicate compiles cleanly
            # at metadata-creation time without needing the table to be
            # autoloaded first.
            sqlite_where=text("run_id IS NOT NULL"),
        ),
        {"sqlite_with_rowid": True, "info": _STRICT_INFO},
    )

    id: str = Field(
        default_factory=_uuid7_str,
        sa_column=Column("id", String(36), primary_key=True, nullable=False),
    )
    run_id: str | None = Field(
        default=None,
        sa_column=Column(
            "run_id",
            String(36),
            ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=True,
            index=True,
        ),
    )
    # EventKind enum value stored as TEXT — keeping it as a plain string lets
    # the database remain readable from any SQLite client without an enum
    # translation layer. The repository layer is responsible for coercing to
    # the EventKind StrEnum on read.
    kind: str = Field(
        sa_column=Column("kind", String, nullable=False, index=True),
    )
    producer_id: str = Field(
        sa_column=Column(
            "producer_id",
            String(36),
            ForeignKey("producers.id"),
            nullable=False,
            index=True,
        ),
    )
    payload_json: str = Field(
        default="{}",
        sa_column=Column("payload_json", String, nullable=False, default="{}"),
    )
    # ``created_at`` is indexed because the most common access pattern is "last
    # N events globally" or "events since timestamp T". ISO-8601 strings sort
    # correctly under SQLite's default BINARY collation.
    created_at: str = Field(
        default_factory=_iso_now_ms,
        sa_column=Column("created_at", String, nullable=False, index=True),
    )
    # Nullable because run-less events (e.g. a global ``producer_registered``)
    # have no run to be monotonic against. When run_id IS NOT NULL the
    # partial UNIQUE index above enforces strict per-run monotonicity.
    seq: int | None = Field(
        default=None,
        sa_column=Column("seq", Integer, nullable=True),
    )


# ---------------------------------------------------------------------------
# EventDeliveryTable
# ---------------------------------------------------------------------------


class EventDeliveryTable(SQLModel, table=True):
    """Per-(event, consumer) delivery record.

    The composite PRIMARY KEY ``(event_id, consumer_id)`` makes the at-most-once
    guarantee structural: the schema itself cannot represent two delivery
    records for the same (event, consumer) pair. The bus uses this row as
    both the delivery ledger and the retry counter.

    Indices
    -------
    * ``idx_event_deliveries_consumer_pending`` —
      ``(consumer_id, status, delivered_at)``. Designed for the bus's poll
      loop, which asks "give me pending deliveries for consumer C, oldest
      first". Putting ``status`` second lets the planner use the prefix for
      both "everything for C" and "pending for C" queries.

    ``status`` defaults to ``DeliveryStatus.pending``; we store the StrEnum
    value as TEXT and let the repository layer wrap it back into the enum on
    read, matching the convention used for ``EventTable.kind``.

    ``attempts`` starts at 0; the bus increments it on each delivery attempt
    regardless of outcome so a runaway consumer is easy to spot.
    """

    __tablename__ = "event_deliveries"
    __table_args__ = (
        Index(
            "idx_event_deliveries_consumer_pending",
            "consumer_id",
            "status",
            "delivered_at",
        ),
        {"sqlite_with_rowid": True, "info": _STRICT_INFO},
    )

    event_id: str = Field(
        sa_column=Column(
            "event_id",
            String(36),
            ForeignKey("events.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
    )
    consumer_id: str = Field(
        sa_column=Column(
            "consumer_id",
            String(36),
            ForeignKey("consumers.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
    )
    delivered_at: str | None = Field(
        default=None,
        sa_column=Column("delivered_at", String, nullable=True),
    )
    acked_at: str | None = Field(
        default=None,
        sa_column=Column("acked_at", String, nullable=True),
    )
    # Storing the StrEnum value as plain TEXT keeps STRICT mode happy and
    # avoids tying the schema to the Python enum class — adding a new
    # DeliveryStatus member is a code change only, not a migration.
    status: str = Field(
        default="pending",
        sa_column=Column(
            "status",
            String,
            nullable=False,
            default="pending",
            index=True,
        ),
    )
    attempts: int = Field(
        default=0,
        sa_column=Column("attempts", Integer, nullable=False, default=0),
    )
