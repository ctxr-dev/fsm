"""Unit tests for ``ctxr.fsm.sqlite.connection``.

Coverage targets:

* PRAGMAs are applied on every fresh DB-API connection — ``journal_mode=wal``,
  ``busy_timeout=5000``, ``foreign_keys=1``, ``synchronous=1`` (NORMAL).
* :func:`ensure_strict_tables` correctly reports per-table STRICT state and
  surfaces the SQLite version.
* Re-opening an existing database file preserves all previously-written
  data (the engine + PRAGMA listener do not destroy state).
* Concurrent reads succeed under WAL — multiple sessions can read in
  parallel without lock-contention errors.

These are pure unit tests: every test isolates state via
``tempfile.TemporaryDirectory()`` so nothing leaks between cases.
"""

from __future__ import annotations

import tempfile
import threading
from pathlib import Path

import pytest
from sqlalchemy import text

from ctxr.fsm.sqlite import (
    Project,
    detect_journal_state,
    ensure_strict_tables,
    open_engine,
    open_session,
)

# ---------------------------------------------------------------------------
# PRAGMA installation
# ---------------------------------------------------------------------------


def test_open_engine_applies_journal_mode_wal() -> None:
    """The connect listener must switch the DB into WAL journalling mode."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fsm.db"
        engine = open_engine(db_path)
        try:
            state = detect_journal_state(engine)
            # SQLite reports journal_mode case-insensitively; normalise.
            assert str(state["journal_mode"]).lower() == "wal"
        finally:
            engine.dispose()


def test_open_engine_applies_busy_timeout() -> None:
    """``busy_timeout`` must be raised to 5000 ms so short locks don't fail."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fsm.db"
        engine = open_engine(db_path)
        try:
            state = detect_journal_state(engine)
            assert int(state["busy_timeout"]) == 5000
        finally:
            engine.dispose()


def test_open_engine_enables_foreign_keys() -> None:
    """SQLite defaults FK enforcement to OFF; our listener must turn it ON."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fsm.db"
        engine = open_engine(db_path)
        try:
            state = detect_journal_state(engine)
            assert int(state["foreign_keys"]) == 1
        finally:
            engine.dispose()


def test_open_engine_sets_synchronous_normal() -> None:
    """``synchronous`` must be NORMAL (numeric value 1) — WAL's sweet spot."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fsm.db"
        engine = open_engine(db_path)
        try:
            state = detect_journal_state(engine)
            assert int(state["synchronous"]) == 1
        finally:
            engine.dispose()


def test_pragmas_applied_to_every_new_connection() -> None:
    """The connect listener must run for every fresh connection in the pool,
    not just the first one. We open two distinct connections and assert each
    reports the expected PRAGMA values independently."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fsm.db"
        engine = open_engine(db_path)
        try:
            # Open + close + reopen forces the engine to hand out a fresh
            # DB-API connection on the second connect() (or reuse one from
            # the pool whose listener fired at creation time). Either way,
            # the observed PRAGMA values must remain correct.
            for _ in range(2):
                with engine.connect() as conn:
                    journal_mode = conn.execute(text("PRAGMA journal_mode")).scalar()
                    foreign_keys = conn.execute(text("PRAGMA foreign_keys")).scalar()
                    assert str(journal_mode).lower() == "wal"
                    assert int(foreign_keys) == 1
        finally:
            engine.dispose()


# ---------------------------------------------------------------------------
# ensure_strict_tables reporting
# ---------------------------------------------------------------------------


def test_ensure_strict_tables_reports_on_empty_db() -> None:
    """A freshly-created DB has no user tables — the report's table lists
    must be empty but the SQLite version and ``supports_strict`` flag
    must still be populated."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fsm.db"
        engine = open_engine(db_path)
        try:
            report = ensure_strict_tables(engine)
            assert report["strict"] == []
            assert report["non_strict"] == []
            assert isinstance(report["sqlite_version"], str)
            assert report["sqlite_version"].count(".") >= 2
            # We're on Python 3.12 which ships SQLite >= 3.37 — STRICT must
            # be reported as supported.
            assert report["supports_strict"] is True
        finally:
            engine.dispose()


def test_ensure_strict_tables_classifies_strict_vs_non_strict() -> None:
    """When a STRICT and a non-STRICT table coexist, the report must place
    each into the correct bucket and warn for the non-STRICT one."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fsm.db"
        engine = open_engine(db_path)
        try:
            with engine.begin() as conn:
                conn.execute(
                    text("CREATE TABLE strict_one (id TEXT PRIMARY KEY) STRICT")
                )
                conn.execute(
                    text("CREATE TABLE loose_one (id TEXT PRIMARY KEY)")
                )

            with pytest.warns(UserWarning, match="loose_one"):
                report = ensure_strict_tables(engine)

            assert "strict_one" in report["strict"]
            assert "loose_one" in report["non_strict"]
            assert "strict_one" not in report["non_strict"]
            assert "loose_one" not in report["strict"]
        finally:
            engine.dispose()


def test_ensure_strict_tables_after_project_open_marks_all_strict() -> None:
    """Running migrations via ``Project.open`` produces a fully-STRICT
    schema for every ctxr-owned table. (Alembic's own ``alembic_version``
    bookkeeping table is owned by Alembic and is not STRICT — we
    deliberately ignore it in this check.)"""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fsm.db"
        with Project.open(db_path) as proj:
            # The migration emits the expected UserWarning for the
            # Alembic-owned table; tolerate it explicitly so a future
            # ``filterwarnings = error`` config doesn't turn this test red.
            with pytest.warns(UserWarning, match="alembic_version"):
                report = ensure_strict_tables(proj.engine)

            project_non_strict = [
                name for name in report["non_strict"] if name != "alembic_version"
            ]
            assert project_non_strict == [], (
                f"unexpected non-STRICT ctxr tables after migration: "
                f"{project_non_strict}"
            )
            # Sanity check: at least one user table exists after migration.
            assert len(report["strict"]) > 0


# ---------------------------------------------------------------------------
# Re-opening preserves data
# ---------------------------------------------------------------------------


def test_reopen_preserves_data() -> None:
    """Closing the engine and re-opening the same file must surface all
    previously-committed rows."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fsm.db"

        # First open: register a project, commit, close.
        with (
            Project.open(db_path) as proj,
            proj.session_factory() as session,
            session.begin(),
        ):
            proj.projects.create(session, slug="alpha")
            proj.projects.create(session, slug="beta")

        # Second open: data must still be visible.
        with Project.open(db_path) as proj, proj.session_factory() as session:
            projects = proj.projects.list(session)
            slugs = sorted(p.slug for p in projects)
            assert slugs == ["alpha", "beta"]


def test_reopen_preserves_pragmas() -> None:
    """Re-opening the database must re-apply the project PRAGMAs (they are
    set on every connection, not persisted with the file)."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fsm.db"

        engine = open_engine(db_path)
        try:
            with open_session(engine) as session:
                session.execute(text("CREATE TABLE marker (id TEXT PRIMARY KEY)"))
                session.commit()
        finally:
            engine.dispose()

        # Reopen and verify PRAGMAs are reapplied AND the table is still
        # there.
        engine = open_engine(db_path)
        try:
            state = detect_journal_state(engine)
            assert str(state["journal_mode"]).lower() == "wal"
            assert int(state["foreign_keys"]) == 1
            assert int(state["busy_timeout"]) == 5000
            assert int(state["synchronous"]) == 1

            with engine.connect() as conn:
                names = [
                    row[0]
                    for row in conn.execute(
                        text(
                            "SELECT name FROM sqlite_master "
                            "WHERE type='table' AND name='marker'"
                        )
                    ).all()
                ]
            assert names == ["marker"]
        finally:
            engine.dispose()


# ---------------------------------------------------------------------------
# Concurrent reads under WAL
# ---------------------------------------------------------------------------


def test_concurrent_reads_under_wal() -> None:
    """WAL must allow multiple readers in parallel without lock errors.

    We spin up several worker threads that each open their own session,
    read the seeded row, and report the value back. With WAL + the
    project PRAGMAs, every read must succeed and return the same row.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fsm.db"
        engine = open_engine(db_path)
        try:
            with engine.begin() as conn:
                conn.execute(
                    text("CREATE TABLE items (id TEXT PRIMARY KEY, payload TEXT)")
                )
                conn.execute(
                    text("INSERT INTO items (id, payload) VALUES ('only', 'hello')")
                )

            num_readers = 8
            results: list[str | None] = [None] * num_readers
            errors: list[BaseException] = []
            barrier = threading.Barrier(num_readers)

            def reader(idx: int) -> None:
                try:
                    # Stagger less; coordinate via the barrier so every
                    # thread issues SELECT at roughly the same instant.
                    barrier.wait(timeout=5.0)
                    with engine.connect() as conn:
                        value = conn.execute(
                            text("SELECT payload FROM items WHERE id = 'only'")
                        ).scalar_one()
                    results[idx] = value
                except BaseException as exc:
                    errors.append(exc)

            threads = [
                threading.Thread(target=reader, args=(i,))
                for i in range(num_readers)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10.0)

            assert errors == [], f"concurrent reads raised: {errors}"
            assert results == ["hello"] * num_readers
        finally:
            engine.dispose()


def test_concurrent_reads_during_write_under_wal() -> None:
    """WAL's defining feature: a reader is not blocked by an in-flight
    writer. We start a write transaction, leave it open, and prove that a
    concurrent read on a separate connection still succeeds."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fsm.db"
        engine = open_engine(db_path)
        try:
            with engine.begin() as conn:
                conn.execute(
                    text("CREATE TABLE items (id TEXT PRIMARY KEY, payload TEXT)")
                )
                conn.execute(
                    text("INSERT INTO items (id, payload) VALUES ('only', 'before')")
                )

            writer_started = threading.Event()
            writer_may_commit = threading.Event()
            writer_error: list[BaseException] = []

            def writer() -> None:
                try:
                    with engine.connect() as conn, conn.begin():
                        conn.execute(
                            text(
                                "UPDATE items SET payload = 'after' "
                                "WHERE id = 'only'"
                            )
                        )
                        writer_started.set()
                        # Hold the write txn open until the test allows
                        # it to commit; the reader must observe the
                        # pre-update value during this window.
                        assert writer_may_commit.wait(timeout=5.0)
                except BaseException as exc:
                    writer_error.append(exc)
                    # Make sure the main thread is unblocked even on error.
                    writer_started.set()

            writer_thread = threading.Thread(target=writer)
            writer_thread.start()
            try:
                assert writer_started.wait(timeout=5.0)

                # WAL contract: this read should NOT block on the writer.
                with engine.connect() as conn:
                    value = conn.execute(
                        text("SELECT payload FROM items WHERE id = 'only'")
                    ).scalar_one()
                # Reader sees the pre-commit snapshot ('before') because
                # the writer hasn't committed yet — that is precisely the
                # WAL snapshot-isolation guarantee.
                assert value == "before"
            finally:
                writer_may_commit.set()
                writer_thread.join(timeout=5.0)

            assert writer_error == [], f"writer raised: {writer_error}"

            # After the writer commits, a fresh reader sees the new value.
            with engine.connect() as conn:
                final = conn.execute(
                    text("SELECT payload FROM items WHERE id = 'only'")
                ).scalar_one()
            assert final == "after"
        finally:
            engine.dispose()
