"""initial schema (core lifecycle + event bus + enforcement substrate)

Revision ID: 0001_initial
Revises:
Create Date: 2026-05-30

This is the SQLite-bootstrap migration for ctxr.fsm. It creates every table
declared by the three SQLModel modules under ``ctxr.fsm.sqlite``:

* ``models_core``         — projects, fsm_specs, runs, run_sessions, states,
                            transitions, worker_artifacts, aggregates, locks.
* ``models_events``       — producers, consumers, events, event_deliveries.
* ``models_enforcement``  — journal_txns, tool_calls, drift_signals,
                            commit_signatures, commit_tokens.

Why hand-rolled DDL instead of ``op.create_table``?
---------------------------------------------------
SQLite's STRICT modifier (introduced 3.37, 2021-11) imposes two constraints
that interact poorly with the default SQLAlchemy DDL compiler:

1. Only the five primitive storage classes are accepted: TEXT, INTEGER, REAL,
   BLOB, ANY. Synthetic types the dialect normally emits — VARCHAR, BOOLEAN,
   FLOAT — are rejected at CREATE TABLE time with
   ``unknown datatype for X.col: "VARCHAR(36)"``.
2. STRICT must appear at CREATE TABLE time; there is no ALTER ... SET STRICT.

The ``models_events`` and ``models_enforcement`` modules declare some
columns with ``String(36)``, ``Boolean``, and ``Float`` because they predate
the strict-types decision; rewriting them in the migration is the
project-agreed bridge until those modules are refactored. The
``models_core`` module already uses ``Text``/``Integer`` exclusively and
sets ``sqlite_strict=True`` natively, so for consistency this migration
emits ALL tables via raw SQL — there is then exactly one source of truth
for the on-disk shape.

Downgrade ordering
------------------
Tables are dropped in reverse-dependency order: leaf tables first
(event_deliveries → events → consumers, drift_signals → producers, etc.),
parent tables last (runs → projects). FOREIGN KEY constraints are not
strictly enforced during ``DROP TABLE`` even with ``foreign_keys=ON``, but
ordering the drops keeps the migration readable and avoids relying on
that quirk.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_initial"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# CREATE TABLE statements (all STRICT).
# Listed in dependency order so each FK references an already-created table.
# ---------------------------------------------------------------------------


_CREATE_TABLES: tuple[tuple[str, str], ...] = (
    # -------------------------------------------------------------------
    # models_core
    # -------------------------------------------------------------------
    (
        "projects",
        """
        CREATE TABLE projects (
            id TEXT NOT NULL,
            slug TEXT NOT NULL,
            created_at TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (id)
        ) STRICT
        """,
    ),
    (
        "fsm_specs",
        """
        CREATE TABLE fsm_specs (
            id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            slug TEXT NOT NULL,
            version INTEGER NOT NULL,
            hash TEXT NOT NULL,
            definition_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (id),
            CONSTRAINT idx_fsm_specs_unique UNIQUE (project_id, slug, version),
            CONSTRAINT ck_fsm_specs_version_positive CHECK (version >= 1),
            FOREIGN KEY (project_id) REFERENCES projects (id) ON DELETE CASCADE
        ) STRICT
        """,
    ),
    (
        "runs",
        """
        CREATE TABLE runs (
            id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            fsm_spec_id TEXT NOT NULL,
            fsm_spec_hash TEXT NOT NULL,
            status TEXT NOT NULL,
            current_state TEXT,
            next_state TEXT,
            verdict TEXT,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            last_update_at TEXT NOT NULL,
            paused_at TEXT,
            pause_reason TEXT,
            parent_run_id TEXT,
            resume_history_json TEXT NOT NULL DEFAULT '[]',
            args_json TEXT NOT NULL DEFAULT '{}',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            transitions_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (id),
            FOREIGN KEY (project_id) REFERENCES projects (id),
            FOREIGN KEY (fsm_spec_id) REFERENCES fsm_specs (id),
            FOREIGN KEY (parent_run_id) REFERENCES runs (id)
        ) STRICT
        """,
    ),
    (
        "run_sessions",
        """
        CREATE TABLE run_sessions (
            id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            acquired_at TEXT NOT NULL,
            released_at TEXT,
            release_reason TEXT,
            PRIMARY KEY (id),
            FOREIGN KEY (run_id) REFERENCES runs (id) ON DELETE CASCADE
        ) STRICT
        """,
    ),
    (
        "states",
        """
        CREATE TABLE states (
            id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            state_id TEXT NOT NULL,
            entry_seq INTEGER NOT NULL,
            entered_at TEXT NOT NULL,
            exited_at TEXT,
            status TEXT NOT NULL,
            inputs_json TEXT NOT NULL DEFAULT '{}',
            outputs_json TEXT NOT NULL DEFAULT '{}',
            iteration_n INTEGER,
            PRIMARY KEY (id),
            FOREIGN KEY (run_id) REFERENCES runs (id) ON DELETE CASCADE
        ) STRICT
        """,
    ),
    (
        "transitions",
        """
        CREATE TABLE transitions (
            id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            from_state_id TEXT NOT NULL,
            to_state_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            predicate TEXT,
            predicate_result INTEGER,
            decided_at TEXT NOT NULL,
            PRIMARY KEY (id),
            FOREIGN KEY (run_id) REFERENCES runs (id) ON DELETE CASCADE,
            FOREIGN KEY (from_state_id) REFERENCES states (id)
        ) STRICT
        """,
    ),
    (
        "worker_artifacts",
        """
        CREATE TABLE worker_artifacts (
            id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            state_id TEXT NOT NULL,
            iteration_n INTEGER,
            prompt_text TEXT NOT NULL,
            prompt_hash TEXT NOT NULL,
            output_json TEXT NOT NULL,
            validated INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            PRIMARY KEY (id),
            FOREIGN KEY (run_id) REFERENCES runs (id) ON DELETE CASCADE,
            FOREIGN KEY (state_id) REFERENCES states (id)
        ) STRICT
        """,
    ),
    (
        "aggregates",
        """
        CREATE TABLE aggregates (
            id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            field TEXT NOT NULL,
            from_state_ids_json TEXT NOT NULL,
            merged_length INTEGER NOT NULL,
            items_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (id),
            FOREIGN KEY (run_id) REFERENCES runs (id) ON DELETE CASCADE
        ) STRICT
        """,
    ),
    (
        "locks",
        """
        CREATE TABLE locks (
            run_id TEXT NOT NULL,
            holder_session_id TEXT NOT NULL,
            acquired_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            PRIMARY KEY (run_id),
            FOREIGN KEY (run_id) REFERENCES runs (id) ON DELETE CASCADE
        ) STRICT
        """,
    ),
    # -------------------------------------------------------------------
    # models_events — VARCHAR / BOOLEAN / FLOAT rewritten to TEXT / INTEGER /
    # REAL for STRICT compatibility. The ORM still uses String(36) etc., which
    # is benign because STRICT only inspects the literal type-name at CREATE
    # TABLE time; round-tripped values continue to be plain TEXT/INTEGER on
    # disk.
    # -------------------------------------------------------------------
    (
        "producers",
        """
        CREATE TABLE producers (
            id TEXT NOT NULL,
            kind TEXT NOT NULL,
            name TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            PRIMARY KEY (id),
            CONSTRAINT uq_producers_kind_name UNIQUE (kind, name)
        ) STRICT
        """,
    ),
    (
        "consumers",
        """
        CREATE TABLE consumers (
            id TEXT NOT NULL,
            kind TEXT NOT NULL,
            name TEXT NOT NULL,
            filter_kind TEXT,
            filter_run_id TEXT,
            created_at TEXT NOT NULL,
            last_seen_at TEXT,
            PRIMARY KEY (id),
            CONSTRAINT uq_consumers_kind_name UNIQUE (kind, name),
            FOREIGN KEY (filter_run_id) REFERENCES runs (id) ON DELETE CASCADE
        ) STRICT
        """,
    ),
    (
        "events",
        """
        CREATE TABLE events (
            id TEXT NOT NULL,
            run_id TEXT,
            kind TEXT NOT NULL,
            producer_id TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            seq INTEGER,
            PRIMARY KEY (id),
            FOREIGN KEY (run_id) REFERENCES runs (id) ON DELETE CASCADE,
            FOREIGN KEY (producer_id) REFERENCES producers (id)
        ) STRICT
        """,
    ),
    (
        "event_deliveries",
        """
        CREATE TABLE event_deliveries (
            event_id TEXT NOT NULL,
            consumer_id TEXT NOT NULL,
            delivered_at TEXT,
            acked_at TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (event_id, consumer_id),
            FOREIGN KEY (event_id) REFERENCES events (id) ON DELETE CASCADE,
            FOREIGN KEY (consumer_id) REFERENCES consumers (id) ON DELETE CASCADE
        ) STRICT
        """,
    ),
    # -------------------------------------------------------------------
    # models_enforcement — same STRICT-typing rewrites as the event tables.
    # ``succeeded`` and ``verified`` are stored as INTEGER 0/1; ``weight`` is
    # REAL. Callers continue to use Python bool/float — SQLAlchemy round-trips
    # the values cleanly through the type-coercion shim.
    # -------------------------------------------------------------------
    (
        "journal_txns",
        """
        CREATE TABLE journal_txns (
            id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            status TEXT NOT NULL,
            staged_writes_json TEXT NOT NULL DEFAULT '[]',
            started_at TEXT NOT NULL,
            ready_at TEXT,
            finalised_at TEXT,
            PRIMARY KEY (id),
            FOREIGN KEY (run_id) REFERENCES runs (id) ON DELETE CASCADE
        ) STRICT
        """,
    ),
    (
        "tool_calls",
        """
        CREATE TABLE tool_calls (
            id TEXT NOT NULL,
            run_id TEXT,
            producer_id TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            args_redacted_json TEXT NOT NULL DEFAULT '{}',
            succeeded INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (id),
            FOREIGN KEY (run_id) REFERENCES runs (id) ON DELETE CASCADE,
            FOREIGN KEY (producer_id) REFERENCES producers (id)
        ) STRICT
        """,
    ),
    (
        "drift_signals",
        """
        CREATE TABLE drift_signals (
            id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            producer_id TEXT NOT NULL,
            signal_kind TEXT NOT NULL,
            weight REAL NOT NULL DEFAULT 1.0,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            PRIMARY KEY (id),
            FOREIGN KEY (run_id) REFERENCES runs (id) ON DELETE CASCADE,
            FOREIGN KEY (producer_id) REFERENCES producers (id)
        ) STRICT
        """,
    ),
    (
        "commit_signatures",
        """
        CREATE TABLE commit_signatures (
            id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            state_id TEXT NOT NULL,
            iteration_n INTEGER,
            brief_id TEXT NOT NULL,
            inputs_hash TEXT NOT NULL,
            outputs_hash TEXT NOT NULL,
            session_id TEXT NOT NULL,
            signature TEXT NOT NULL,
            verified INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (id),
            FOREIGN KEY (run_id) REFERENCES runs (id) ON DELETE CASCADE,
            FOREIGN KEY (state_id) REFERENCES states (id)
        ) STRICT
        """,
    ),
    (
        "commit_tokens",
        """
        CREATE TABLE commit_tokens (
            token TEXT NOT NULL,
            run_id TEXT NOT NULL,
            state_id TEXT NOT NULL,
            expected_next_state TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            consumed_at TEXT,
            PRIMARY KEY (token),
            FOREIGN KEY (run_id) REFERENCES runs (id) ON DELETE CASCADE
        ) STRICT
        """,
    ),
)


# ---------------------------------------------------------------------------
# Index declarations.
#
# Each entry is (index_name, table_name, op.create_index kwargs).
# ``columns`` and ``unique`` are passed positionally; ``sqlite_where`` is
# forwarded for partial indexes (only ``idx_events_run_seq`` uses it).
#
# We use ``op.create_index`` (not raw SQL) so the index DDL stays
# dialect-portable and so alembic's autogenerate diffing keeps working for
# future migrations that touch indexes only.
# ---------------------------------------------------------------------------


_INDEXES: tuple[dict, ...] = (
    # models_core ---------------------------------------------------------
    {"name": "ix_projects_slug", "table": "projects", "columns": ["slug"], "unique": True},
    {"name": "ix_fsm_specs_project_id", "table": "fsm_specs", "columns": ["project_id"]},
    {"name": "ix_fsm_specs_slug", "table": "fsm_specs", "columns": ["slug"]},
    {"name": "ix_runs_project_id", "table": "runs", "columns": ["project_id"]},
    {"name": "ix_runs_fsm_spec_id", "table": "runs", "columns": ["fsm_spec_id"]},
    {"name": "ix_runs_status", "table": "runs", "columns": ["status"]},
    {"name": "ix_runs_started_at", "table": "runs", "columns": ["started_at"]},
    {"name": "ix_runs_last_update_at", "table": "runs", "columns": ["last_update_at"]},
    {
        "name": "idx_runs_status_last_update",
        "table": "runs",
        # ``last_update_at DESC`` so ORDER BY ... DESC LIMIT n is index-only.
        "columns_sql": "status, last_update_at DESC",
    },
    {
        "name": "idx_runs_project_started",
        "table": "runs",
        "columns_sql": "project_id, started_at DESC",
    },
    {"name": "idx_runs_parent", "table": "runs", "columns": ["parent_run_id"]},
    {"name": "ix_run_sessions_run_id", "table": "run_sessions", "columns": ["run_id"]},
    {"name": "ix_run_sessions_session_id", "table": "run_sessions", "columns": ["session_id"]},
    {
        "name": "idx_run_sessions_run",
        "table": "run_sessions",
        "columns": ["run_id", "session_id"],
    },
    {"name": "ix_states_run_id", "table": "states", "columns": ["run_id"]},
    {
        "name": "idx_states_run_seq",
        "table": "states",
        "columns": ["run_id", "entry_seq"],
        "unique": True,
    },
    {
        "name": "idx_transitions_run_from",
        "table": "transitions",
        "columns": ["run_id", "from_state_id"],
    },
    # models_events -------------------------------------------------------
    {"name": "ix_events_run_id", "table": "events", "columns": ["run_id"]},
    {"name": "ix_events_kind", "table": "events", "columns": ["kind"]},
    {"name": "ix_events_producer_id", "table": "events", "columns": ["producer_id"]},
    {"name": "ix_events_created_at", "table": "events", "columns": ["created_at"]},
    {
        # Partial UNIQUE: per-run monotonic seq is enforced only for events
        # bound to a run. Run-less events live on a global timeline keyed
        # solely by created_at, so they are exempt from the (run_id, seq)
        # uniqueness check.
        "name": "idx_events_run_seq",
        "table": "events",
        "columns": ["run_id", "seq"],
        "unique": True,
        "sqlite_where_sql": "run_id IS NOT NULL",
    },
    {"name": "ix_consumers_kind", "table": "consumers", "columns": ["kind"]},
    {"name": "ix_event_deliveries_status", "table": "event_deliveries", "columns": ["status"]},
    {
        "name": "idx_event_deliveries_consumer_pending",
        "table": "event_deliveries",
        "columns": ["consumer_id", "status", "delivered_at"],
    },
    # models_enforcement --------------------------------------------------
    {"name": "ix_journal_txns_run_id", "table": "journal_txns", "columns": ["run_id"]},
    {"name": "ix_journal_txns_status", "table": "journal_txns", "columns": ["status"]},
    {
        "name": "idx_journal_run_status",
        "table": "journal_txns",
        "columns": ["run_id", "status"],
    },
    {"name": "ix_tool_calls_run_id", "table": "tool_calls", "columns": ["run_id"]},
    {"name": "ix_tool_calls_producer_id", "table": "tool_calls", "columns": ["producer_id"]},
    {"name": "ix_tool_calls_tool_name", "table": "tool_calls", "columns": ["tool_name"]},
    {"name": "ix_tool_calls_created_at", "table": "tool_calls", "columns": ["created_at"]},
    {"name": "ix_drift_signals_run_id", "table": "drift_signals", "columns": ["run_id"]},
    {
        "name": "ix_drift_signals_signal_kind",
        "table": "drift_signals",
        "columns": ["signal_kind"],
    },
    {
        "name": "ix_commit_signatures_run_id",
        "table": "commit_signatures",
        "columns": ["run_id"],
    },
    {
        "name": "idx_commit_signatures_run",
        "table": "commit_signatures",
        "columns": ["run_id", "created_at"],
    },
    {"name": "ix_commit_tokens_run_id", "table": "commit_tokens", "columns": ["run_id"]},
    {
        "name": "ix_commit_tokens_expires_at",
        "table": "commit_tokens",
        "columns": ["expires_at"],
    },
)


def _create_index(spec: dict) -> None:
    """Create an index from one entry in ``_INDEXES``.

    Two flavours are supported:

    * ``columns`` (list[str]) — plain ascending columns; emitted via
      ``op.create_index`` so alembic owns the dialect-specific DDL.
    * ``columns_sql`` (str) — raw SQL fragment for the column list, used
      when we need ``DESC`` ordering (SQLite-specific syntax that
      ``op.create_index`` does not expose directly).

    ``sqlite_where_sql`` adds a partial-index predicate.
    """
    name = spec["name"]
    table = spec["table"]
    unique = spec.get("unique", False)

    if "columns_sql" in spec:
        unique_kw = "UNIQUE " if unique else ""
        op.execute(
            f"CREATE {unique_kw}INDEX {name} ON {table} ({spec['columns_sql']})"
        )
        return

    if "sqlite_where_sql" in spec:
        # Build raw SQL so we can attach the WHERE clause; op.create_index
        # supports sqlite_where but only via a SQLAlchemy expression object,
        # which requires an autoloaded Table — overkill for one partial idx.
        cols = ", ".join(spec["columns"])
        unique_kw = "UNIQUE " if unique else ""
        op.execute(
            f"CREATE {unique_kw}INDEX {name} ON {table} ({cols}) "
            f"WHERE {spec['sqlite_where_sql']}"
        )
        return

    op.create_index(
        name,
        table,
        spec["columns"],
        unique=unique,
    )


# ---------------------------------------------------------------------------
# Upgrade / Downgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    """Create every table (STRICT) and its associated indexes."""
    for _name, ddl in _CREATE_TABLES:
        # ``op.execute`` accepts a plain string; we strip surrounding
        # whitespace so the generated SQL is one tidy statement per table.
        op.execute(ddl.strip())

    for spec in _INDEXES:
        _create_index(spec)


def downgrade() -> None:
    """Drop every table in reverse dependency order.

    Indexes attached to a table are automatically dropped when the table is
    dropped, so we do not enumerate them separately on the downgrade path.
    """
    for name, _ddl in reversed(_CREATE_TABLES):
        op.execute(f"DROP TABLE IF EXISTS {name}")
