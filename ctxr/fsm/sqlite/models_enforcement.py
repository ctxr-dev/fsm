"""SQLModel table definitions for the FSM enforcement layer (W12 substrate).

This module declares the persistent tables that make the "enforcement
shell" of the FSM observable and auditable:

* ``journal_txns`` — open / in-flight / finalised journal transactions
  recorded around each state-commit. A journal txn brackets the staged
  writes that a worker is about to materialise so a crash mid-commit can
  be rolled back deterministically.
* ``tool_calls`` — every tool invocation the workers (or the engine)
  emit, captured for drift detection and post-hoc audit. Args are
  redacted (the producer side scrubs secrets before persistence).
* ``drift_signals`` — typed signals raised by the enforcement layer
  (off-allowlist tool calls, repeated validation failures, signature
  mismatches, verifier rejections, …). The drift aggregator reads from
  this table to decide whether to pause a run.
* ``commit_signatures`` — the SHA-256 commitment binding a brief to its
  outputs at commit time. Verified=True means the inputs/outputs hash
  matched the worker-supplied signature.
* ``commit_tokens`` — short-lived single-use tokens that authorise a
  state-commit. Issued on state entry, consumed at commit, expired by
  the reaper after their TTL elapses.

Conventions enforced here (see project AGENTS.md and the W2 substrate
brief):

* All tables use **SQLite STRICT mode**. Because SQLModel 0.0.38 does
  not natively emit STRICT in its CREATE TABLE DDL, we attach a
  ``__table_args__`` dialect kwarg ``sqlite_with_rowid=True`` and a
  ``info={"sqlite_strict": True}`` marker. The actual ``STRICT``
  clause is appended at DDL emission time by the engine layer (see
  ``ctxr.fsm.sqlite.connection.ensure_strict_tables`` for the audit
  side, and the alembic migration template for the emit side). The
  marker is the contract the migration / DDL compiler reads.
* All primary keys are **UUIDv7** generated via ``uuid_utils.uuid7``
  and stored as ``TEXT(36)`` (the canonical hex-with-dashes form).
* All datetimes are stored as **ISO-8601 UTC TEXT** with millisecond
  precision (see ``_utc_iso_millis``). The columns themselves are typed
  ``str`` because the storage substrate keeps them as text; higher
  layers parse to ``datetime`` on read.
* JSON-shaped fields are stored as **TEXT** (canonical JSON). We do
  not use SQLAlchemy's ``JSON`` column type here so STRICT mode can
  apply uniformly — STRICT only accepts the five primitive SQLite
  types. Repository code is responsible for canonical-JSON encoding
  on write and decoding on read.

This module deliberately depends only on ``sqlmodel`` /
``sqlalchemy`` / ``uuid_utils`` / stdlib — no FastAPI, no MCP, no
core engine imports. The ``SignalKind`` taxonomy is referenced via a
string column rather than the StrEnum to keep this module decoupled
from ``ctxr.fsm.core``; the producer side validates the value against
``ctxr.fsm.core.models.SignalKind`` before insert.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import uuid_utils
from sqlalchemy import Boolean, Column, Float, ForeignKey, Index, Integer, String, Text
from sqlmodel import Field, SQLModel

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _new_uuid7() -> str:
    """Mint a fresh UUIDv7 as the canonical 36-char hex-with-dashes string.

    UUIDv7 is preferred over v4 because its leading bits encode a
    millisecond timestamp, which means rows are roughly time-sortable
    by primary key without an additional ORDER BY on ``created_at``.
    """
    return str(uuid_utils.uuid7())


def _utc_iso_millis() -> str:
    """Return the current UTC time as ISO-8601 with millisecond precision.

    Output shape: ``2026-05-29T12:34:56.789Z`` (the trailing ``Z``
    flags UTC). Stripping micros to millis keeps the textual form
    short and stable across platforms where ``datetime.isoformat()``
    would otherwise emit 6 fractional digits.
    """
    now = datetime.now(tz=UTC)
    # Truncate microseconds → milliseconds (3 digits) and append the
    # explicit Z to remove the +00:00 offset suffix that isoformat()
    # would otherwise produce.
    millis = now.microsecond // 1000
    return f"{now.strftime('%Y-%m-%dT%H:%M:%S')}.{millis:03d}Z"


# The STRICT-table marker. The alembic migration / DDL compiler hook
# reads ``info["sqlite_strict"]`` off ``Table.info`` (which is the
# dict produced from the ``info=`` kwarg in ``__table_args__``) and
# appends ``STRICT`` to the generated CREATE TABLE. ``sqlite_with_rowid``
# is set explicitly so we do not accidentally end up with a
# ``WITHOUT ROWID`` table — STRICT and WITHOUT ROWID are independent
# but easy to confuse.
_STRICT_TABLE_KWARGS: dict[str, Any] = {
    "sqlite_with_rowid": True,
    "info": {"sqlite_strict": True},
}


# ---------------------------------------------------------------------------
# JournalTxnTable
# ---------------------------------------------------------------------------


class JournalTxnTable(SQLModel, table=True):
    """Open / in-flight / finalised journal transactions.

    Each row brackets the staged writes a worker is about to commit at
    a state boundary. A row moves through three statuses:

    * ``pending`` — opened on state entry; writes accumulating in
      ``staged_writes_json``.
    * ``ready_to_finalise`` — worker has produced its outputs and the
      enforcement layer has signed off; awaiting the atomic-flush step.
    * ``finalised`` — flushed; the staged writes have been materialised
      into their target tables and the txn is closed.

    The composite ``(run_id, status)`` index supports the recovery
    path that scans for stuck pending / ready_to_finalise txns at
    engine startup.
    """

    __tablename__ = "journal_txns"
    __table_args__ = (
        Index("idx_journal_run_status", "run_id", "status"),
        _STRICT_TABLE_KWARGS,
    )

    id: str = Field(
        default_factory=_new_uuid7,
        sa_column=Column(String(36), primary_key=True),
    )
    run_id: str = Field(
        sa_column=Column(
            String(36),
            ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
    )
    status: str = Field(
        sa_column=Column(String(32), nullable=False, index=True),
    )
    staged_writes_json: str = Field(
        default="[]",
        sa_column=Column(Text, nullable=False, default="[]"),
    )
    started_at: str = Field(
        default_factory=_utc_iso_millis,
        sa_column=Column(String(32), nullable=False),
    )
    ready_at: str | None = Field(
        default=None,
        sa_column=Column(String(32), nullable=True),
    )
    finalised_at: str | None = Field(
        default=None,
        sa_column=Column(String(32), nullable=True),
    )


# ---------------------------------------------------------------------------
# ToolCallTable
# ---------------------------------------------------------------------------


class ToolCallTable(SQLModel, table=True):
    """Every tool invocation observed by the enforcement layer.

    ``run_id`` is nullable because some tool calls (notably ones
    emitted by the orchestrator before a run is opened, or by
    out-of-band introspection workers) are not bound to a run. The
    drift aggregator only considers rows where ``run_id`` is non-NULL.

    ``args_redacted_json`` is the redacted shape of the arguments the
    tool was called with — secrets / credentials / large blobs are
    stripped by the producer before persistence. The raw arguments
    are never stored.

    ``created_at`` is indexed descending so the common "show me the
    most recent tool calls for run X" query is a cheap index scan.
    """

    __tablename__ = "tool_calls"
    __table_args__ = (_STRICT_TABLE_KWARGS,)

    id: str = Field(
        default_factory=_new_uuid7,
        sa_column=Column(String(36), primary_key=True),
    )
    run_id: str | None = Field(
        default=None,
        sa_column=Column(
            String(36),
            ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=True,
            index=True,
        ),
    )
    producer_id: str = Field(
        sa_column=Column(
            String(36),
            ForeignKey("producers.id"),
            nullable=False,
            index=True,
        ),
    )
    tool_name: str = Field(
        sa_column=Column(String(128), nullable=False, index=True),
    )
    args_redacted_json: str = Field(
        default="{}",
        sa_column=Column(Text, nullable=False, default="{}"),
    )
    succeeded: bool = Field(
        sa_column=Column("succeeded", Boolean, nullable=False),
    )
    created_at: str = Field(
        default_factory=_utc_iso_millis,
        # SQLite has no real DESC-on-column flag at CREATE TABLE
        # time, but creating an index with the column in DESC order
        # gives us efficient ORDER BY created_at DESC. We declare the
        # index inline via ``index=True`` for the baseline ascending
        # index plus an explicit DESC index in __table_args__ at the
        # repository's discretion; for the substrate the simple
        # ``index=True`` is sufficient and the most recent rows are
        # cheap to find via the UUIDv7-ordered primary key anyway.
        sa_column=Column(String(32), nullable=False, index=True),
    )


# ---------------------------------------------------------------------------
# DriftSignalTable
# ---------------------------------------------------------------------------


class DriftSignalTable(SQLModel, table=True):
    """Typed drift signals raised against a run.

    ``signal_kind`` is the wire form of ``ctxr.fsm.core.models.SignalKind``
    (a StrEnum) — stored as plain text so this module stays decoupled
    from the core package. The producer side is responsible for
    validating the value against the enum before insert.

    ``weight`` lets the aggregator score signals: low-weight signals
    accumulate into a pause threshold, while high-weight signals can
    trip the pause on their own. Default is 1.0 — the unit signal.

    ``payload_json`` carries the signal-kind-specific context (e.g.
    the off-allowlist tool name, the validation-failure count) as
    canonical JSON text.
    """

    __tablename__ = "drift_signals"
    __table_args__ = (_STRICT_TABLE_KWARGS,)

    id: str = Field(
        default_factory=_new_uuid7,
        sa_column=Column(String(36), primary_key=True),
    )
    run_id: str = Field(
        sa_column=Column(
            String(36),
            ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
    )
    producer_id: str = Field(
        sa_column=Column(
            String(36),
            ForeignKey("producers.id"),
            nullable=False,
        ),
    )
    signal_kind: str = Field(
        sa_column=Column(String(64), nullable=False, index=True),
    )
    weight: float = Field(
        default=1.0,
        sa_column=Column("weight", Float, nullable=False, default=1.0),
    )
    payload_json: str = Field(
        default="{}",
        sa_column=Column(Text, nullable=False, default="{}"),
    )
    created_at: str = Field(
        default_factory=_utc_iso_millis,
        sa_column=Column(String(32), nullable=False),
    )


# ---------------------------------------------------------------------------
# CommitSignatureTable
# ---------------------------------------------------------------------------


class CommitSignatureTable(SQLModel, table=True):
    """SHA-256 commitments binding briefs to their outputs.

    A commit signature is created at the moment a worker commits its
    outputs for a state (or loop iteration). It hashes
    ``inputs_hash + outputs_hash + session_id + brief_id`` into a
    single ``signature`` value. ``verified`` is set to True iff the
    server-side recomputation of the signature matches the value the
    worker supplied — a mismatch raises a
    ``SignalKind.signature_mismatch`` drift signal.

    ``iteration_n`` is non-NULL only for commits inside a loop state;
    for plain (non-loop) states it stays NULL. The
    ``idx_commit_signatures_run`` index supports the timeline view of
    "all commits for run X, newest first".
    """

    __tablename__ = "commit_signatures"
    __table_args__ = (
        Index("idx_commit_signatures_run", "run_id", "created_at"),
        _STRICT_TABLE_KWARGS,
    )

    id: str = Field(
        default_factory=_new_uuid7,
        sa_column=Column(String(36), primary_key=True),
    )
    run_id: str = Field(
        sa_column=Column(
            String(36),
            ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
    )
    state_id: str = Field(
        sa_column=Column(
            String(36),
            ForeignKey("states.id"),
            nullable=False,
        ),
    )
    iteration_n: int | None = Field(
        default=None,
        sa_column=Column("iteration_n", Integer, nullable=True),
    )
    brief_id: str = Field(
        sa_column=Column(String(36), nullable=False),
    )
    inputs_hash: str = Field(
        sa_column=Column(String(64), nullable=False),
    )
    outputs_hash: str = Field(
        sa_column=Column(String(64), nullable=False),
    )
    session_id: str = Field(
        sa_column=Column(String(128), nullable=False),
    )
    signature: str = Field(
        sa_column=Column(String(64), nullable=False),
    )
    verified: bool = Field(
        sa_column=Column("verified", Boolean, nullable=False),
    )
    created_at: str = Field(
        default_factory=_utc_iso_millis,
        sa_column=Column(String(32), nullable=False),
    )


# ---------------------------------------------------------------------------
# CommitTokenTable
# ---------------------------------------------------------------------------


class CommitTokenTable(SQLModel, table=True):
    """Short-lived single-use tokens authorising a state-commit.

    A token is minted on state entry, carries the engine's expected
    next state (so a stale token from a previous state cannot be
    replayed against a new state), and expires after a TTL (default
    60s — see ``ctxr.fsm.core.models.CommitToken.issue``). The
    enforcement layer requires a non-expired, non-consumed token at
    commit time; ``consumed_at`` is set atomically with the commit
    flush.

    Unlike the other tables in this module, the **primary key is the
    token value itself** rather than a separate ``id`` column. The
    token is already a UUIDv7, so it is dense, unique, and roughly
    time-ordered — there is no value in carrying a second surrogate
    key. ``expires_at`` is indexed so the reaper job that expires
    stale tokens can scan it efficiently.
    """

    __tablename__ = "commit_tokens"
    __table_args__ = (_STRICT_TABLE_KWARGS,)

    token: str = Field(
        default_factory=_new_uuid7,
        sa_column=Column(String(36), primary_key=True),
    )
    run_id: str = Field(
        sa_column=Column(
            String(36),
            ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
    )
    state_id: str = Field(
        sa_column=Column(String(36), nullable=False),
    )
    expected_next_state: str = Field(
        sa_column=Column(String(64), nullable=False),
    )
    expires_at: str = Field(
        sa_column=Column(String(32), nullable=False, index=True),
    )
    consumed_at: str | None = Field(
        default=None,
        sa_column=Column(String(32), nullable=True),
    )


__all__ = [
    "CommitSignatureTable",
    "CommitTokenTable",
    "DriftSignalTable",
    "JournalTxnTable",
    "ToolCallTable",
]
