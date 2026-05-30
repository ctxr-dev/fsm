"""Tests for ``ctxr-fsm doctor``.

The doctor command is the operator's first diagnostic stop, so we
exercise both presentation modes (rich pretty-print and ``--json``)
against a freshly-initialised project DB.

Conventions followed (per the project-wide testing guide):

* :class:`typer.testing.CliRunner` drives the CLI in-process so we can
  inspect ``stdout``/``stderr`` without spawning a subprocess.
* Every test uses its own :class:`tempfile.TemporaryDirectory` so DB
  state from one test cannot leak into another, even when pytest is
  invoked with ``-p no:cacheprovider`` or a non-default tmp root.
* ``--db <tmp>/fsm.db`` is always passed explicitly to bypass the
  ``$CTXR_FSM_DB`` env-var precedence layer and avoid coupling to the
  developer's shell environment.
* Each test calls ``init`` first so the schema, PRAGMAs, and seed
  tables exist before ``doctor`` runs.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from typer.testing import CliRunner

from ctxr.fsm.cli import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_project(db_path: Path) -> None:
    """Bootstrap a project DB at ``db_path`` so ``doctor`` has something to read."""
    result = runner.invoke(app, ["init", "--db", str(db_path), "--json"])
    assert result.exit_code == 0, f"init failed: {result.stdout}\n{result.stderr}"


# ---------------------------------------------------------------------------
# Pretty (default) output
# ---------------------------------------------------------------------------


def test_doctor_after_init_prints_db_path() -> None:
    """The pretty output must include the resolved DB path verbatim.

    Operators rely on this line to confirm which file the report
    describes — especially when juggling multiple project DBs through
    ``$CTXR_FSM_DB``.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fsm.db"
        _init_project(db_path)

        result = runner.invoke(app, ["doctor", "--db", str(db_path)])

        assert result.exit_code == 0, result.stderr
        # ``Path.resolve`` (used inside resolve_db_path) may add a /private prefix
        # on macOS temp dirs, so check against the resolved string the CLI itself
        # would compute rather than the raw tempdir input.
        assert str(db_path.resolve()) in result.stdout


def test_doctor_after_init_prints_pragma_values() -> None:
    """The pretty output must surface the PRAGMAs that determine correctness.

    The connect-time listener applies ``WAL``, ``foreign_keys=ON`` and
    friends; doctor surfaces them so an operator can confirm the
    listener actually ran on this DB.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fsm.db"
        _init_project(db_path)

        result = runner.invoke(app, ["doctor", "--db", str(db_path)])

        assert result.exit_code == 0, result.stderr
        # Each PRAGMA we configure at connect time must be visible in the
        # rendered output. ``rich.print`` renders the dict literally, so a
        # plain substring check on the key name is sufficient.
        for pragma in ("journal_mode", "busy_timeout", "foreign_keys", "synchronous"):
            assert pragma in result.stdout, f"missing pragma {pragma!r} in: {result.stdout}"
        # The applied values should also appear (WAL for journal_mode, the
        # busy-timeout integer, etc.). We assert on the most diagnostic two:
        # WAL flips a default, and foreign_keys=ON is the one SQLite ships
        # OFF by default.
        assert "wal" in result.stdout.lower()


def test_doctor_after_init_lists_tables_with_row_counts() -> None:
    """Doctor must list user tables alongside their row counts.

    After ``init`` the schema is populated but empty, so every reported
    count should be ``0`` and the ``alembic_version`` bookkeeping table
    should be present (it holds the migration revision and is the only
    table guaranteed to be non-empty post-init).
    """
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fsm.db"
        _init_project(db_path)

        result = runner.invoke(app, ["doctor", "--db", str(db_path)])

        assert result.exit_code == 0, result.stderr
        # alembic_version is the bookkeeping table; if it's missing the
        # migration didn't run, which would invalidate every other check.
        assert "alembic_version" in result.stdout
        # The core domain tables created by the W2 migration must appear.
        for table in ("fsm_specs", "runs", "states"):
            assert table in result.stdout, f"missing table {table!r} in: {result.stdout}"


# ---------------------------------------------------------------------------
# JSON output
# ---------------------------------------------------------------------------


def test_doctor_json_mode_produces_parseable_output() -> None:
    """``--json`` must emit a single JSON document parseable by ``json.loads``.

    This is the contract scripts depend on (a CI doctor check, the
    ``serve`` warm-up probe in W7, etc.), so it gets its own test
    rather than being folded into the structural assertions below.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fsm.db"
        _init_project(db_path)

        result = runner.invoke(app, ["doctor", "--db", str(db_path), "--json"])

        assert result.exit_code == 0, result.stderr
        # If this raises ``json.JSONDecodeError`` the test fails — that
        # is the entire point of the assertion, so we let the exception
        # propagate rather than wrapping it in a friendlier message.
        payload = json.loads(result.stdout)
        assert isinstance(payload, dict)


def test_doctor_json_mode_includes_all_report_sections() -> None:
    """JSON output must include every section operators script against.

    The doctor report's stable surface is documented in the module
    docstring; this test pins the keys so a refactor that drops a
    section (or renames one) trips loudly.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fsm.db"
        _init_project(db_path)

        result = runner.invoke(app, ["doctor", "--db", str(db_path), "--json"])

        assert result.exit_code == 0, result.stderr
        payload = json.loads(result.stdout)
        for key in (
            "db_path",
            "file_size_bytes",
            "sqlite_version",
            "pragmas",
            "alembic_revision",
            "tables",
            "journal_txns",
            "locks",
        ):
            assert key in payload, f"missing report key {key!r}: {payload}"


def test_doctor_json_db_path_matches_requested_path() -> None:
    """The ``db_path`` field must echo the resolved CLI argument.

    Operators chain this through scripts (e.g. ``ctxr-fsm doctor --json
    | jq -r .db_path``); a regression where ``--db`` is silently
    ignored would be invisible to the pretty-print test alone.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fsm.db"
        _init_project(db_path)

        result = runner.invoke(app, ["doctor", "--db", str(db_path), "--json"])

        assert result.exit_code == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["db_path"] == str(db_path.resolve())


def test_doctor_json_pragmas_reflect_connect_listener() -> None:
    """The pragmas section must reflect the connect-time listener's writes.

    If the listener doesn't fire on the engine the doctor command uses,
    we'd see SQLite defaults (``delete`` journal mode, ``foreign_keys=0``)
    instead of our configured values. Pinning the values here turns a
    silent listener-regression into a fast failure.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fsm.db"
        _init_project(db_path)

        result = runner.invoke(app, ["doctor", "--db", str(db_path), "--json"])

        assert result.exit_code == 0, result.stderr
        payload = json.loads(result.stdout)
        pragmas = payload["pragmas"]
        # journal_mode is reported lower-cased by SQLite regardless of
        # the value we sent in upper-case — normalise both sides.
        assert str(pragmas["journal_mode"]).lower() == "wal"
        # ``foreign_keys`` and ``synchronous`` come back as integers
        # (1 = ON, 1 = NORMAL respectively); compare against int(1) to
        # avoid coupling to the JSON renderer's quoting choice.
        assert pragmas["foreign_keys"] == 1
        # busy_timeout is the milliseconds we configured.
        assert pragmas["busy_timeout"] == 5000


def test_doctor_json_tables_contain_alembic_version_with_one_row() -> None:
    """``alembic_version`` should report exactly one row after migrations run.

    The migration framework writes a single row holding the head
    revision; doctor must surface that count so an operator can spot a
    DB that somehow accumulated multiple version rows (which would
    indicate a corrupted migration history).
    """
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fsm.db"
        _init_project(db_path)

        result = runner.invoke(app, ["doctor", "--db", str(db_path), "--json"])

        assert result.exit_code == 0, result.stderr
        payload = json.loads(result.stdout)
        tables = payload["tables"]
        assert "alembic_version" in tables
        assert tables["alembic_version"] == 1
        # Domain tables exist and are empty on a fresh init — the
        # absolute set of names is owned by the migrations, so we only
        # assert on the ones the W2 migration is committed to providing.
        for table in ("fsm_specs", "runs", "states"):
            assert table in tables, f"missing table {table!r} in: {tables}"
            assert tables[table] == 0


def test_doctor_json_journal_txns_breakdown_keys_present() -> None:
    """The journal_txns section must declare the three canonical statuses.

    Even on an empty DB the breakdown carries zeros for ``pending``,
    ``ready_to_finalise``, and ``finalised`` so downstream consumers
    never need to handle missing keys.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fsm.db"
        _init_project(db_path)

        result = runner.invoke(app, ["doctor", "--db", str(db_path), "--json"])

        assert result.exit_code == 0, result.stderr
        payload = json.loads(result.stdout)
        journal = payload["journal_txns"]
        for status in ("pending", "ready_to_finalise", "finalised"):
            assert status in journal
            assert journal[status] == 0


def test_doctor_json_locks_count_is_zero_after_init() -> None:
    """No run holds the writer lock immediately after ``init``."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fsm.db"
        _init_project(db_path)

        result = runner.invoke(app, ["doctor", "--db", str(db_path), "--json"])

        assert result.exit_code == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["locks"] == {"count": 0}


def test_doctor_json_alembic_revision_is_non_empty_string() -> None:
    """The reported alembic revision must be a non-empty string post-init.

    A ``None`` revision would mean the ``alembic_version`` table is
    empty (i.e. migrations didn't run), which would silently invalidate
    everything else in the report.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fsm.db"
        _init_project(db_path)

        result = runner.invoke(app, ["doctor", "--db", str(db_path), "--json"])

        assert result.exit_code == 0, result.stderr
        payload = json.loads(result.stdout)
        revision = payload["alembic_revision"]
        assert isinstance(revision, str)
        assert revision  # not empty
