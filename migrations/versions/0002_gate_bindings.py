"""W23g: gate_bindings table for cross-FSM gate resolution audit + topology

Revision ID: 0002_gate_bindings
Revises: 0001_initial
Create Date: 2026-06-01

Adds one new STRICT table:

* ``gate_bindings`` — one row per resolved gate state. Carries the
  full binding (target run + state entry seq, source run + state +
  field, target field, source kind, resolved value JSON, timestamps)
  so the dashboard can render the cross-run topology and the audit
  log survives independently of the events stream.

The table follows the same STRICT + UUIDv7 + ISO-8601 UTC TEXT
conventions as every other table in the schema. Three indexes:

* ``idx_gate_bindings_by_target`` — powers the Bindings panel on
  ``/runs/:id`` showing the run's INCOMING gates.
* ``idx_gate_bindings_by_source`` — powers the symmetric OUTGOING
  view: "which downstream runs pulled outputs FROM this run?".
* ``idx_gate_bindings_resolved_at`` — powers ``/links`` topology
  paging by recency.

Why hand-rolled DDL: see the 0001_initial migration's docstring.
SQLite's STRICT mode requires exactly the five primitive storage
classes; hand-rolled CREATE TABLE keeps the on-disk shape under one
source of truth and avoids the SQLAlchemy dialect emitting VARCHAR /
BOOLEAN / FLOAT synthetic types.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002_gate_bindings"
down_revision: str | Sequence[str] | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CREATE_GATE_BINDINGS_SQL = """
CREATE TABLE gate_bindings (
    id TEXT NOT NULL,
    target_run_id TEXT NOT NULL,
    target_state_entry_seq INTEGER NOT NULL,
    target_field TEXT NOT NULL,
    source_run_id TEXT,
    source_spec_slug TEXT,
    source_state_id TEXT NOT NULL,
    source_field TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    resolved_value_json TEXT NOT NULL DEFAULT 'null',
    resolved_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY (target_run_id) REFERENCES runs (id) ON DELETE CASCADE,
    FOREIGN KEY (source_run_id) REFERENCES runs (id) ON DELETE SET NULL
) STRICT
"""


_INDEXES: tuple[tuple[str, str, str], ...] = (
    # (index name, table name, column list)
    ("idx_gate_bindings_by_target", "gate_bindings", "target_run_id"),
    ("idx_gate_bindings_by_source", "gate_bindings", "source_run_id"),
    ("idx_gate_bindings_resolved_at", "gate_bindings", "resolved_at"),
    ("ix_gate_bindings_source_kind", "gate_bindings", "source_kind"),
)


def upgrade() -> None:
    op.execute(_CREATE_GATE_BINDINGS_SQL.strip())
    for name, table, columns in _INDEXES:
        op.execute(f"CREATE INDEX {name} ON {table} ({columns})")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS gate_bindings")
