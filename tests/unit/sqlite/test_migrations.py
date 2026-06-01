"""Unit tests for the Alembic migration stack of ``ctxr.fsm.sqlite``.

These tests exercise the migration runner end-to-end against a temp-dir
SQLite database so we cover:

* ``alembic upgrade head`` on a clean (empty) DB creates every expected
  table at the right revision.
* ``alembic downgrade base`` reverses the upgrade cleanly — every project
  table is dropped and the ``alembic_version`` row goes away.
* ``alembic upgrade head`` is idempotent: running it twice does not
  raise ("table already exists") and leaves the schema unchanged.
* At least three sampled tables carry the SQLite STRICT clause — proven
  by scanning ``sqlite_master.sql`` for the trailing ``STRICT`` keyword.

The public migration helper exercised here is
``ctxr.fsm.sqlite.run_migrations`` (re-exported from
``ctxr.fsm.sqlite.project``). For the explicit downgrade path we build
the same ``alembic.config.Config`` shape the helper uses and call
``alembic.command.downgrade``; there is no public downgrade wrapper
because production code never reverses migrations.

Test isolation: each test gets its own ``tempfile.TemporaryDirectory``
so no DB file is ever shared, and we restore any ``CTXR_FSM_DB_URL``
mutation the helper might have left behind in this process.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig

from ctxr.fsm.sqlite import run_migrations

# ---------------------------------------------------------------------------
# Constants extracted from migrations/versions/0001_initial.py.
#
# Kept inline (rather than imported from the migration module) so that if
# the migration is rewritten or split into multiple revisions the assertion
# fails loudly here and forces the test author to confirm the new schema
# was intentional.
# ---------------------------------------------------------------------------

# Tables created by the 0001_initial revision (in dependency order). The
# count is what the "expected table count" assertion checks against.
_EXPECTED_TABLES: frozenset[str] = frozenset(
    {
        # models_core
        "projects",
        "fsm_specs",
        "runs",
        "run_sessions",
        "states",
        "transitions",
        "worker_artifacts",
        "aggregates",
        "locks",
        # models_events
        "producers",
        "consumers",
        "events",
        "event_deliveries",
        # models_enforcement
        "journal_txns",
        "tool_calls",
        "drift_signals",
        "commit_signatures",
        "commit_tokens",
        # models_gates (W23g)
        "gate_bindings",
    }
)

# Three tables we deliberately sample to confirm the STRICT clause is
# emitted. We pick one from each model module so a regression in any one
# of them is caught:
#   * ``projects``       — models_core
#   * ``events``         — models_events
#   * ``commit_tokens``  — models_enforcement
_STRICT_SAMPLES: tuple[str, ...] = ("projects", "events", "commit_tokens")

# Revision label of the head migration; mirrors
# ``migrations/versions/0001_initial.py::revision``.
_HEAD_REVISION: str = "0002_gate_bindings"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _alembic_ini_path() -> Path:
    """Locate the repo's ``alembic.ini`` from this test file's location.

    Walks up from ``tests/unit/sqlite/test_migrations.py`` until a sibling
    ``alembic.ini`` is found. This mirrors what ``Project._find_alembic_ini``
    does, but we keep an independent copy so the test does not depend on
    a private helper.
    """
    here = Path(__file__).resolve()
    for candidate in (here, *here.parents):
        ini = candidate / "alembic.ini"
        if ini.is_file():
            return ini
    raise FileNotFoundError("alembic.ini not found from test location")


def _make_alembic_config(db_path: Path) -> AlembicConfig:
    """Build an ``alembic.config.Config`` pointed at ``db_path``.

    Used for direct ``command.upgrade`` / ``command.downgrade`` calls when
    the test needs finer-grained control than ``run_migrations`` exposes
    (in particular, the downgrade path).
    """
    ini = _alembic_ini_path()
    cfg = AlembicConfig(str(ini))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _user_tables(db_path: Path) -> set[str]:
    """Return the set of non-system tables in ``db_path``.

    Filters out ``alembic_version`` and SQLite's internal ``sqlite_*``
    tables so the caller sees only the schema's own tables.
    """
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' "
            "AND name != 'alembic_version'"
        ).fetchall()
    return {row[0] for row in rows}


def _alembic_version(db_path: Path) -> str | None:
    """Return the current ``alembic_version`` row, or ``None`` if absent.

    Used both to confirm the upgrade succeeded (row equals the head
    revision) and to confirm the downgrade cleared the version table
    (no row at all).
    """
    with sqlite3.connect(db_path) as conn:
        try:
            row = conn.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()
        except sqlite3.OperationalError:
            # ``alembic_version`` does not exist yet — equivalent to "no
            # migration has ever run against this DB".
            return None
    return row[0] if row else None


def _table_ddl(db_path: Path, table: str) -> str:
    """Return the ``CREATE TABLE`` DDL for ``table`` from ``sqlite_master``.

    SQLite stores the original DDL verbatim (including the STRICT clause
    when present), so checking the suffix of this string is the canonical
    way to verify STRICT-mode was honoured.
    """
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
    assert row is not None, f"table {table!r} not found in sqlite_master"
    return row[0]


@pytest.fixture()
def restore_env() -> object:
    """Snapshot ``CTXR_FSM_DB_URL`` and restore it after the test.

    ``run_migrations`` mutates this env var for the duration of its call
    and restores it on exit, so this fixture is belt-and-braces in case
    a future regression leaks the mutation; it also keeps the test suite
    hermetic if another test inadvertently sets the var.
    """
    previous = os.environ.get("CTXR_FSM_DB_URL")
    yield
    if previous is None:
        os.environ.pop("CTXR_FSM_DB_URL", None)
    else:
        os.environ["CTXR_FSM_DB_URL"] = previous


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_upgrade_head_creates_all_expected_tables(restore_env: object) -> None:
    """``upgrade head`` on a clean DB creates every table at the head rev."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fsm.db"

        run_migrations(db_path)

        tables = _user_tables(db_path)
        # Assert exact equality (not subset) so a renamed / added table
        # forces the test author to update _EXPECTED_TABLES intentionally.
        assert tables == _EXPECTED_TABLES, (
            f"unexpected table set; "
            f"missing={_EXPECTED_TABLES - tables}, "
            f"extra={tables - _EXPECTED_TABLES}"
        )
        # Sanity: the explicit expected count (18 tables) is what the spec
        # asks us to verify, so we assert it directly as well.
        assert len(tables) == 19

        # ``alembic_version`` should now carry the head revision.
        assert _alembic_version(db_path) == _HEAD_REVISION


def test_downgrade_base_reverses_upgrade(restore_env: object) -> None:
    """``downgrade base`` drops every project table and clears the version row."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fsm.db"

        run_migrations(db_path)
        # Pre-condition: upgrade left us at the head revision with the full
        # table set. If this fails the test below is meaningless, so we
        # check it eagerly.
        assert _user_tables(db_path) == _EXPECTED_TABLES
        assert _alembic_version(db_path) == _HEAD_REVISION

        cfg = _make_alembic_config(db_path)
        # ``CTXR_FSM_DB_URL`` is what migrations/env.py actually reads, so
        # set it for the downgrade as well (run_migrations does this on
        # the upgrade path; we mirror it here for symmetry).
        os.environ["CTXR_FSM_DB_URL"] = f"sqlite:///{db_path}"
        alembic_command.downgrade(cfg, "base")

        # Every project table must be gone.
        assert _user_tables(db_path) == set()
        # ``alembic_version`` exists but holds no row after downgrade-to-base.
        assert _alembic_version(db_path) is None


def test_upgrade_head_is_idempotent(restore_env: object) -> None:
    """Re-running ``upgrade head`` is a no-op (no exceptions, same schema)."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fsm.db"

        run_migrations(db_path)
        first_tables = _user_tables(db_path)
        first_version = _alembic_version(db_path)

        # The second invocation must not raise ("table already exists"
        # would mean alembic ran the migration again, which is the
        # regression we are guarding against).
        run_migrations(db_path)
        second_tables = _user_tables(db_path)
        second_version = _alembic_version(db_path)

        assert first_tables == second_tables == _EXPECTED_TABLES
        assert first_version == second_version == _HEAD_REVISION


def test_sampled_tables_declare_strict(restore_env: object) -> None:
    """At least three sampled tables carry the SQLite STRICT clause.

    SQLite preserves the CREATE TABLE DDL verbatim in ``sqlite_master.sql``,
    so checking that the stored statement ends with ``STRICT`` (after
    whitespace normalisation) is the canonical way to confirm the table
    was created strictly.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fsm.db"

        run_migrations(db_path)

        # Defence in depth — three samples is the spec requirement.
        assert len(_STRICT_SAMPLES) >= 3

        for table in _STRICT_SAMPLES:
            ddl = _table_ddl(db_path, table)
            # Strip trailing whitespace / semicolons before comparing so
            # we are robust to either ``... ) STRICT`` or ``... ) STRICT;``
            # being stored.
            normalised = ddl.rstrip().rstrip(";").rstrip()
            assert normalised.endswith("STRICT"), (
                f"table {table!r} is missing the STRICT clause; "
                f"DDL was: {ddl!r}"
            )
