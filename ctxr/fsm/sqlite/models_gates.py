"""SQLModel table for W23g cross-FSM gate bindings.

A gate state pauses a run waiting for a value supplied from outside the
run's own environment. When the value comes from another run's state
output (the ``run_output`` source kind), a row in this table records
the binding so:

* The dashboard can render the cross-run topology (``/links`` route,
  Bindings panels on ``/runs/:id`` + ``/specs/:id``).
* A future run reusing the same binding shape can resolve faster
  without re-deriving the source.
* The audit trail of "which run pulled which output from which other
  run" survives independently of the events stream.

The shape mirrors :class:`ctxr.fsm.core.models.GateBinding` plus the
target-run / target-state-entry-seq + resolved-value + timestamps the
persistence layer owns. See ``ctxr/fsm/memory/GATE_CONTRACT.md`` for
the protocol; this module is the storage half.

Conventions enforced (same as ``models_enforcement.py``):

* SQLite STRICT mode (``__table_args__`` carries the
  ``sqlite_strict`` marker the alembic migration reads).
* UUIDv7 primary keys stored as ``TEXT(36)``.
* ISO-8601 UTC TEXT timestamps with millisecond precision.
* JSON payloads stored as canonical-JSON TEXT (no SQLAlchemy JSON
  column type, so STRICT mode applies uniformly).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import uuid_utils
from sqlalchemy import Column, ForeignKey, Index, Integer, String, Text
from sqlmodel import Field, SQLModel


def _new_uuid7() -> str:
    """Mint a fresh UUIDv7 in the canonical 36-char hex-with-dashes form."""

    return str(uuid_utils.uuid7())


def _utc_iso_millis() -> str:
    """Return the current UTC time as ISO-8601 with millisecond precision.

    Matches the helper used by every other ctxr.fsm.sqlite table so the
    on-disk timestamp format stays uniform across the schema.
    """

    now = datetime.now(tz=UTC)
    millis = now.microsecond // 1000
    return f"{now.strftime('%Y-%m-%dT%H:%M:%S')}.{millis:03d}Z"


_STRICT_TABLE_KWARGS: dict[str, Any] = {
    "sqlite_with_rowid": True,
    "info": {"sqlite_strict": True},
}


class GateBindingTable(SQLModel, table=True):
    """W23g: resolved cross-FSM gate bindings.

    One row per resolved gate state. The combination
    ``(target_run_id, target_state_entry_seq)`` is the natural key but
    we keep the surrogate UUIDv7 ``id`` so the row also has a
    time-sortable PK that survives a rerun of the same state (which
    would mint a new entry_seq anyway, but defending against the
    "engine bug double-inserts" case keeps the audit trail honest).

    Indexes:

    * ``idx_gate_bindings_by_target`` — powers the Bindings panel on
      ``/runs/:id`` showing the run's INCOMING gates.
    * ``idx_gate_bindings_by_source`` — powers the symmetric OUTGOING
      view: "which downstream runs pulled outputs FROM this run?".
    * ``idx_gate_bindings_resolved_at`` — powers ``/links`` topology
      paging by recency.
    """

    __tablename__ = "gate_bindings"
    __table_args__ = (
        Index("idx_gate_bindings_by_target", "target_run_id"),
        Index("idx_gate_bindings_by_source", "source_run_id"),
        Index("idx_gate_bindings_resolved_at", "resolved_at"),
        _STRICT_TABLE_KWARGS,
    )

    id: str = Field(
        default_factory=_new_uuid7,
        sa_column=Column(String(36), primary_key=True),
    )
    target_run_id: str = Field(
        sa_column=Column(
            String(36),
            ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
    )
    target_state_entry_seq: int = Field(
        sa_column=Column(Integer, nullable=False),
    )
    target_field: str = Field(
        sa_column=Column(String(255), nullable=False),
    )
    # source_run_id is nullable because the llm_supplied source kind
    # has no source run; the engine still records the binding so the
    # dashboard can show the gate's resolution context. The FK is
    # declared with ON DELETE SET NULL so deleting the source run
    # does not cascade into the downstream run's binding history.
    source_run_id: str | None = Field(
        default=None,
        sa_column=Column(
            String(36),
            ForeignKey("runs.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
    )
    source_spec_slug: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
    )
    source_state_id: str = Field(
        sa_column=Column(String(255), nullable=False),
    )
    source_field: str = Field(
        sa_column=Column(String(255), nullable=False),
    )
    # source_kind is "run_output" or "llm_supplied" (the GateSourceKind
    # enum from the core models). String for the same reason
    # SignalKind is stringly typed in models_enforcement: keeps this
    # module decoupled from ctxr.fsm.core.
    source_kind: str = Field(
        sa_column=Column(String(32), nullable=False, index=True),
    )
    resolved_value_json: str = Field(
        default="null",
        sa_column=Column(Text, nullable=False, default="null"),
    )
    resolved_at: str = Field(
        default_factory=_utc_iso_millis,
        sa_column=Column(String(32), nullable=False),
    )
    created_at: str = Field(
        default_factory=_utc_iso_millis,
        sa_column=Column(String(32), nullable=False),
    )
