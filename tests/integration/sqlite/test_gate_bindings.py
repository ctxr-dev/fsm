"""W23g: tests for the ``gate_bindings`` SQLite table + GatesRepo.

Covers the persistence half of the cross-FSM gate substrate added in
W23g: alembic migration 0002 creates the STRICT table with the right
indexes, the GatesRepo round-trips canonical-JSON resolved values
through the record helper, and the by_target_run / by_source_run /
recent queries return the expected ordering.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from ctxr.fsm.sqlite.project import Project
from ctxr.fsm.sqlite.repos_gates import GateBindingRecord, GatesRepo

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def project_db() -> Iterator[Project]:
    """Yield a freshly-migrated Project against a tmpdir DB."""

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fsm.db"
        project = Project.open(db_path)
        try:
            yield project
        finally:
            project.engine.dispose()


def _seed_run(session: Session, run_id: str) -> None:
    """Insert a minimal runs + projects + fsm_specs row triplet.

    The FK on gate_bindings.target_run_id requires a real runs row;
    rather than wire through the full Project facade for fixture
    seeding we just hand-insert the rows the migration's FK depends
    on. Schema columns mirror migrations/versions/0001_initial.py.

    Project + spec ids are derived from the run_id suffix so multiple
    _seed_run calls within one test do not collide on the projects
    UNIQUE constraint.
    """

    suffix = run_id[:8]
    project_id = f"p{suffix}-{'0' * 27}"[:36]
    spec_id = f"s{suffix}-{'0' * 27}"[:36]
    session.execute(
        text(
            "INSERT INTO projects (id, slug, created_at, metadata_json) "
            "VALUES (:id, :slug, '2026-06-01T00:00:00.000Z', '{}')"
        ),
        {"id": project_id, "slug": f"proj-for-{run_id[:8]}"},
    )
    session.execute(
        text(
            "INSERT INTO fsm_specs "
            "(id, project_id, slug, version, hash, definition_json, created_at) "
            "VALUES (:id, :project_id, 'test-spec', 1, 'h', '{}', "
            "'2026-06-01T00:00:00.000Z')"
        ),
        {"id": spec_id, "project_id": project_id},
    )
    session.execute(
        text(
            "INSERT INTO runs "
            "(id, project_id, fsm_spec_id, fsm_spec_hash, status, "
            "started_at, last_update_at, args_json, metadata_json) "
            "VALUES (:id, :project_id, :spec_id, 'h', 'in_progress', "
            "'2026-06-01T00:00:00.000Z', '2026-06-01T00:00:00.000Z', "
            "'{}', '{}')"
        ),
        {"id": run_id, "project_id": project_id, "spec_id": spec_id},
    )


# ---------------------------------------------------------------------------
# Migration smoke test
# ---------------------------------------------------------------------------


def test_migration_creates_gate_bindings_table_strict(project_db: Project) -> None:
    raw = sqlite3.connect(project_db.engine.url.database)
    try:
        row = raw.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='gate_bindings'"
        ).fetchone()
    finally:
        raw.close()
    assert row is not None, "gate_bindings table is missing post-migration"
    ddl = row[0].upper()
    assert "STRICT" in ddl, f"gate_bindings table is not STRICT: {row[0]}"


def test_migration_creates_expected_indexes(project_db: Project) -> None:
    raw = sqlite3.connect(project_db.engine.url.database)
    try:
        rows = raw.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND tbl_name='gate_bindings' "
            "AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    finally:
        raw.close()
    names = {r[0] for r in rows}
    # Each index documented in models_gates.py + migration 0002 must
    # land in the live schema; the four-name set is the contract.
    expected = {
        "idx_gate_bindings_by_target",
        "idx_gate_bindings_by_source",
        "idx_gate_bindings_resolved_at",
        "ix_gate_bindings_source_kind",
    }
    assert expected.issubset(names), f"missing indexes: {expected - names}"


# ---------------------------------------------------------------------------
# GatesRepo round-trip
# ---------------------------------------------------------------------------


def test_record_round_trip_returns_pydantic_record(project_db: Project) -> None:
    repo = GatesRepo()
    run_id = "11111111-1111-7111-8111-111111111111"
    with project_db.session_factory() as session:
        _seed_run(session, run_id)
        record = repo.record(
            session,
            target_run_id=run_id,
            target_state_entry_seq=4,
            target_field="review_verdict",
            source_state_id="qa",
            source_field="verdict",
            source_kind="llm_supplied",
            resolved_value={"verdict": "GO"},
        )
        session.commit()

    assert isinstance(record, GateBindingRecord)
    assert record.target_run_id == run_id
    assert record.target_state_entry_seq == 4
    assert record.target_field == "review_verdict"
    assert record.source_kind == "llm_supplied"
    assert record.source_run_id is None  # llm_supplied
    assert record.resolved_value == {"verdict": "GO"}
    # UUIDv7 leaves the timestamp identifier in the high bits, so the
    # mint helper always produces 36-char hex-with-dashes.
    assert len(record.id) == 36


def test_canonical_json_encodes_resolved_value_with_sorted_keys(
    project_db: Project,
) -> None:
    repo = GatesRepo()
    run_id = "22222222-2222-7222-8222-222222222222"
    with project_db.session_factory() as session:
        _seed_run(session, run_id)
        repo.record(
            session,
            target_run_id=run_id,
            target_state_entry_seq=1,
            target_field="t",
            source_state_id="s",
            source_field="f",
            source_kind="llm_supplied",
            resolved_value={"b": 2, "a": 1},
        )
        session.commit()

        # Pull the raw JSON out to verify canonical encoding.
        raw = session.execute(
            text(
                "SELECT resolved_value_json FROM gate_bindings "
                "WHERE target_run_id = :rid"
            ),
            {"rid": run_id},
        ).scalar_one()
    # Canonical = sorted keys, compact separators. {"a":1,"b":2}.
    assert raw == '{"a":1,"b":2}'
    # And it round-trips back through json.loads to a plain dict.
    assert json.loads(raw) == {"a": 1, "b": 2}


def test_by_target_run_returns_descending_by_resolved_at(
    project_db: Project,
) -> None:
    repo = GatesRepo()
    run_id = "33333333-3333-7333-8333-333333333333"
    with project_db.session_factory() as session:
        _seed_run(session, run_id)
        for seq in (1, 2, 3):
            repo.record(
                session,
                target_run_id=run_id,
                target_state_entry_seq=seq,
                target_field=f"t{seq}",
                source_state_id="s",
                source_field="f",
                source_kind="llm_supplied",
                resolved_value={"seq": seq},
            )
        session.commit()

        records = repo.by_target_run(session, run_id)

    assert len(records) == 3
    # UUIDv7 + monotonic timestamps: the newest write (seq=3) lands first.
    assert [r.target_state_entry_seq for r in records] == [3, 2, 1]


def test_by_source_run_returns_only_matching_source(
    project_db: Project,
) -> None:
    repo = GatesRepo()
    source = "44444444-4444-7444-8444-444444444444"
    target = "55555555-5555-7555-8555-555555555555"
    other_target = "66666666-6666-7666-8666-666666666666"
    with project_db.session_factory() as session:
        _seed_run(session, source)
        _seed_run(session, target)
        _seed_run(session, other_target)
        # Two bindings FROM source: one to target, one to other_target.
        repo.record(
            session,
            target_run_id=target,
            target_state_entry_seq=1,
            target_field="t",
            source_run_id=source,
            source_state_id="qa",
            source_field="verdict",
            source_kind="run_output",
        )
        repo.record(
            session,
            target_run_id=other_target,
            target_state_entry_seq=1,
            target_field="t",
            source_run_id=source,
            source_state_id="qa",
            source_field="verdict",
            source_kind="run_output",
        )
        # One unrelated binding (no source_run_id) — must not show up.
        repo.record(
            session,
            target_run_id=target,
            target_state_entry_seq=2,
            target_field="t",
            source_state_id="s",
            source_field="f",
            source_kind="llm_supplied",
        )
        session.commit()

        outgoing = repo.by_source_run(session, source)

    assert len(outgoing) == 2
    target_ids = {r.target_run_id for r in outgoing}
    assert target_ids == {target, other_target}


def test_recent_returns_capped_descending(project_db: Project) -> None:
    repo = GatesRepo()
    run_a = "77777777-7777-7777-8777-777777777777"
    run_b = "88888888-8888-7888-8888-888888888888"
    with project_db.session_factory() as session:
        _seed_run(session, run_a)
        _seed_run(session, run_b)
        for run_id in (run_a, run_b):
            for seq in (1, 2):
                repo.record(
                    session,
                    target_run_id=run_id,
                    target_state_entry_seq=seq,
                    target_field=f"t{seq}",
                    source_state_id="s",
                    source_field="f",
                    source_kind="llm_supplied",
                )
        session.commit()

        recent = repo.recent(session, limit=3)

    assert len(recent) == 3
    timestamps = [r.resolved_at for r in recent]
    assert timestamps == sorted(timestamps, reverse=True)


def test_limit_lt_one_returns_empty_list(project_db: Project) -> None:
    repo = GatesRepo()
    with project_db.session_factory() as session:
        assert repo.by_target_run(session, "any", limit=0) == []
        assert repo.by_source_run(session, "any", limit=-1) == []
        assert repo.recent(session, limit=0) == []
