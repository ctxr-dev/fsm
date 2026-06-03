"""W23g: repository for the ``gate_bindings`` table.

The repo follows the same shape as the existing enforcement repos
(:mod:`ctxr.fsm.sqlite.repos_enforcement`):

* Stateless — every method takes a fresh :class:`Session` from the
  caller; the ``@atomic`` decorator owns the transactional context.
* Returns Pydantic record types (:class:`GateBindingRecord`) rather
  than SQLModel rows so callers stay decoupled from the storage shape.
* Canonical-JSON encoded ``resolved_value_json`` so duplicate-detection
  by hash works against a deterministic byte representation.

Read paths surface the two natural queries the dashboard needs:

* :meth:`by_target_run` — INCOMING gates for a run, ordered by
  ``resolved_at DESC``. Powers the Bindings panel on ``/runs/:id``.
* :meth:`by_source_run` — OUTGOING gates from a run (other runs that
  pulled outputs from this one). Same ordering. Powers the symmetric
  panel.
* :meth:`recent` — capped, ordered slice of every binding. Powers the
  ``/links`` topology view.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import uuid_utils
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from ctxr.fsm.sqlite.models_gates import GateBindingTable


def _new_uuid7_str() -> str:
    """Mint a fresh UUIDv7 in the canonical 36-char hex-with-dashes form."""

    return str(uuid_utils.uuid7())


def _utc_iso_millis() -> str:
    """Return current UTC time as ISO-8601 with millisecond precision."""

    now = datetime.now(tz=UTC)
    millis = now.microsecond // 1000
    return f"{now.strftime('%Y-%m-%dT%H:%M:%S')}.{millis:03d}Z"


def _canonical_json(obj: Any) -> str:
    """Encode ``obj`` as canonical JSON (sorted keys, compact separators).

    Same shape every other ctxr.fsm.sqlite repo uses for JSON-shaped
    columns so byte-level equality across rows is stable and signature
    comparisons are deterministic. We deliberately do NOT pass
    ``default=str``: non-JSON-serializable values must raise
    :class:`TypeError` at the boundary so callers cannot silently smuggle
    in objects that stringify non-deterministically and break dedup /
    signature assumptions downstream.
    """

    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


class GateBindingRecord(BaseModel):
    """Public Pydantic view of a single ``gate_bindings`` row.

    The wire shape mirrors the underlying table verbatim; consumers
    (MCP tool, API handler, UI loader) deserialise directly from the
    JSON envelope without re-deriving field names.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    id: str
    target_run_id: str
    target_state_entry_seq: int
    target_field: str
    source_run_id: str | None
    source_spec_slug: str | None
    source_state_id: str
    source_field: str
    source_kind: str
    resolved_value: Any = None
    resolved_at: str
    created_at: str

    @classmethod
    def from_row(cls, row: GateBindingTable) -> GateBindingRecord:
        """Decode a ``GateBindingTable`` row into a public record.

        The JSON column is decoded with :func:`json.loads`; an
        unparseable value defaults to ``None`` rather than raising,
        because the dashboard would rather display "no value" than
        crash the route — and the canonical writer above produces
        deterministic JSON so the only way to get an unparseable value
        in the column is hand-edit.
        """

        try:
            resolved_value = json.loads(row.resolved_value_json)
        except json.JSONDecodeError:
            resolved_value = None
        return cls(
            id=row.id,
            target_run_id=row.target_run_id,
            target_state_entry_seq=row.target_state_entry_seq,
            target_field=row.target_field,
            source_run_id=row.source_run_id,
            source_spec_slug=row.source_spec_slug,
            source_state_id=row.source_state_id,
            source_field=row.source_field,
            source_kind=row.source_kind,
            resolved_value=resolved_value,
            resolved_at=row.resolved_at,
            created_at=row.created_at,
        )


class GatesRepo:
    """CRUD over the ``gate_bindings`` table.

    Stateless; instantiate once per project and reuse across threads /
    async tasks. The caller supplies the ``Session`` so the
    ``@atomic`` decorator on the engine side can group the binding
    record with the same-transaction state-advance writes.
    """

    def record(
        self,
        session: Session,
        *,
        target_run_id: str,
        target_state_entry_seq: int,
        target_field: str,
        source_state_id: str,
        source_field: str,
        source_kind: str,
        source_run_id: str | None = None,
        source_spec_slug: str | None = None,
        resolved_value: Any = None,
    ) -> GateBindingRecord:
        """Persist one resolved gate binding.

        ``source_run_id`` is required when ``source_kind == 'run_output'``;
        callers are expected to enforce that at the resolver layer
        (`fsm.resolve_gate`), so this method does not re-validate — the
        FK is nullable to support the ``llm_supplied`` source kind.
        """

        timestamp = _utc_iso_millis()
        row = GateBindingTable(
            id=_new_uuid7_str(),
            target_run_id=target_run_id,
            target_state_entry_seq=target_state_entry_seq,
            target_field=target_field,
            source_run_id=source_run_id,
            source_spec_slug=source_spec_slug,
            source_state_id=source_state_id,
            source_field=source_field,
            source_kind=source_kind,
            resolved_value_json=_canonical_json(resolved_value),
            resolved_at=timestamp,
            created_at=timestamp,
        )
        session.add(row)
        # Flush so FK / CHECK / STRICT-type violations surface to the
        # caller now rather than at commit time. The @atomic decorator
        # owns the commit.
        session.flush()
        return GateBindingRecord.from_row(row)

    def by_target_run(
        self,
        session: Session,
        target_run_id: str,
        *,
        limit: int = 100,
    ) -> list[GateBindingRecord]:
        """Return INCOMING gates for ``target_run_id`` (most recent first)."""

        if limit < 1:
            return []
        stmt = (
            select(GateBindingTable)
            .where(GateBindingTable.target_run_id == target_run_id)
            .order_by(GateBindingTable.resolved_at.desc(), GateBindingTable.id.desc())
            .limit(limit)
        )
        rows = session.execute(stmt).scalars().all()
        return [GateBindingRecord.from_row(row) for row in rows]

    def by_source_run(
        self,
        session: Session,
        source_run_id: str,
        *,
        limit: int = 100,
    ) -> list[GateBindingRecord]:
        """Return OUTGOING gates from ``source_run_id`` (most recent first)."""

        if limit < 1:
            return []
        stmt = (
            select(GateBindingTable)
            .where(GateBindingTable.source_run_id == source_run_id)
            .order_by(GateBindingTable.resolved_at.desc(), GateBindingTable.id.desc())
            .limit(limit)
        )
        rows = session.execute(stmt).scalars().all()
        return [GateBindingRecord.from_row(row) for row in rows]

    def recent(
        self,
        session: Session,
        *,
        limit: int = 200,
    ) -> list[GateBindingRecord]:
        """Return the most-recent ``limit`` bindings across all runs.

        Powers the ``/links`` topology view's initial paint.
        """

        if limit < 1:
            return []
        stmt = (
            select(GateBindingTable)
            .order_by(GateBindingTable.resolved_at.desc(), GateBindingTable.id.desc())
            .limit(limit)
        )
        rows = session.execute(stmt).scalars().all()
        return [GateBindingRecord.from_row(row) for row in rows]


__all__ = ["GateBindingRecord", "GatesRepo"]
