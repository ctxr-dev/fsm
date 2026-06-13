"""Facade write paths must take SQLite's write-lock up front.

Issue #95: the :class:`Project` facade convenience operations
(``register_spec``, ``start_run``, ``subscribe``, ``commit_and_advance``)
used to open their unit-of-work with ``session.begin()``, which starts
SQLite's *deferred* transaction. A deferred transaction only acquires
the write-lock lazily on the first write, so two concurrent facade
writers can race for the lock at first-write time and surface a spurious
``SQLITE_BUSY`` ("database is locked"). That defeats the serialised
writer guarantee the ``@atomic`` envelope provides via ``BEGIN
IMMEDIATE``.

The fix routes every facade write through ``Project._write_txn``, which
calls the same ``_begin_immediate`` primitive ``@atomic`` relies on.
These tests pin that behaviour:

1. ``test_register_spec_issues_begin_immediate``: the literal ``BEGIN
   IMMEDIATE`` statement is emitted on the connection a facade write
   uses (statement-level proof the lock is taken up front, not
   deferred).
2. ``test_write_txn_holds_write_lock_before_any_write``: while a facade
   ``_write_txn`` block is open and before it has issued any write, a
   *separate* connection that tries its own ``BEGIN IMMEDIATE`` is
   locked out immediately, confirming the write-lock was taken up front
   rather than deferred to first write.

Pure ``ctxr.fsm.sqlite``: we only touch the package's public ``Project``
surface plus the engine SQLAlchemy already exposes via ``proj.engine``.
"""

from __future__ import annotations

import sqlite3
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, event

from ctxr.fsm.core.models import FsmSpec, State
from ctxr.fsm.sqlite import Project

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def project() -> Iterator[Project]:
    """Yield a fresh file-backed :class:`Project` (per-test isolation).

    A real file (not ``:memory:``) is required because the second test
    opens an independent ``sqlite3`` connection to the same database to
    prove cross-connection lock contention.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "fsm.sqlite"
        proj = Project.open(db_path, migrate=True)
        try:
            yield proj
        finally:
            proj.close()


def _minimal_spec() -> FsmSpec:
    """Return the smallest valid :class:`FsmSpec` we can register."""
    return FsmSpec(
        id=f"immediate-spec-{uuid.uuid4().hex[:8]}",
        version=1,
        entry="draft",
        states=[State(id="draft", purpose="placeholder")],
    )


def _capture_statements(engine: Engine, sink: list[str]) -> None:
    """Append every SQL statement executed on ``engine`` to ``sink``."""

    @event.listens_for(engine, "before_cursor_execute")
    def _record(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        sink.append(statement)


def _db_path_from_engine(engine: Engine) -> str:
    """Extract the on-disk SQLite file path from the engine URL."""
    database = engine.url.database
    assert database is not None, "facade engine must be file-backed for this test"
    return database


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_register_spec_issues_begin_immediate(project: Project) -> None:
    """A facade write must emit ``BEGIN IMMEDIATE`` (not a deferred BEGIN)."""
    statements: list[str] = []
    _capture_statements(project.engine, statements)

    # ``register_spec`` is the simplest facade write path; it routes
    # through ``Project._write_txn`` like every other write operation.
    project.register_spec(_minimal_spec(), project_slug="default")

    immediate = [s for s in statements if s.strip().upper() == "BEGIN IMMEDIATE"]
    assert immediate, f"facade write did not issue 'BEGIN IMMEDIATE'; statements seen: {statements}"


def test_write_txn_holds_write_lock_before_any_write(
    project: Project,
) -> None:
    """A facade ``_write_txn`` holds the write-lock from the very first line.

    This is the discriminating property between ``BEGIN IMMEDIATE`` and a
    deferred ``BEGIN``: IMMEDIATE takes the write-lock *up front*, before
    the transaction has issued a single write. We assert the lock is held
    at the top of the ``_write_txn`` block (having issued no INSERT/UPDATE
    of our own yet) by proving a separate connection cannot take its own
    ``BEGIN IMMEDIATE``. Under a deferred BEGIN the lock would not yet
    exist and the rival would succeed.

    The rival connection sets ``busy_timeout=0`` so contention fails
    immediately rather than blocking for the engine's default 5s window,
    keeping the test fast and deterministic.
    """
    db_path = _db_path_from_engine(project.engine)

    rival = sqlite3.connect(db_path, timeout=0)
    rival.execute("PRAGMA busy_timeout=0")
    try:
        # No write is performed inside the block before the assertion.
        # Under a deferred transaction the write-lock would still be
        # unheld here and the rival's BEGIN IMMEDIATE would succeed.
        # Because the facade opens with BEGIN IMMEDIATE, the lock is
        # already held and the rival must be refused.
        with (
            project._write_txn(),
            pytest.raises(sqlite3.OperationalError, match="database is locked"),
        ):
            rival.execute("BEGIN IMMEDIATE")

        # Once the facade transaction has committed and released the
        # lock, the rival can take it without contention.
        rival.execute("BEGIN IMMEDIATE")
        rival.execute("ROLLBACK")
    finally:
        rival.close()
