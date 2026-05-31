"""Tests for ``ctxr-fsm init``.

Coverage:

* ``ctxr-fsm init`` creates ``.ctxr-fsm/fsm.db`` and ``.ctxr-fsm/pids/``
  inside the project dir, and runs ``alembic upgrade head`` so the
  resulting database has an ``alembic_version`` row populated.
* Re-running ``init`` against the same DB is idempotent (no exception,
  no data loss, still reports the correct revision).
* When the current working directory is a git checkout (i.e. has a
  ``.git/`` directory), the command appends ``.ctxr-fsm/`` to
  ``.gitignore`` — creating the file if it does not exist.
* Repeated ``init`` in the same git checkout does NOT duplicate the
  ``.gitignore`` entry — only one ``.ctxr-fsm/`` line ends up in the
  file regardless of how many times the command runs.
* When there is no ``.git/`` directory in the working tree, no
  ``.gitignore`` file is created.

The tests drive the CLI through :class:`typer.testing.CliRunner` —
the same path users hit via the ``ctxr-fsm`` console script — and use
``tempfile.TemporaryDirectory`` for every project DB so the suite is
hermetic and parallel-safe.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from typer.testing import CliRunner

from ctxr.fsm.cli import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def cwd_tmpdir() -> Iterator[Path]:
    """Chdir into a fresh tempdir for the duration of one test.

    The ``init`` command introspects ``Path.cwd()`` to decide whether
    the project lives inside a git checkout, so tests that exercise the
    ``.gitignore`` behaviour need a real (controlled) cwd. We save and
    restore the original cwd so test order does not matter.
    """
    original = Path.cwd()
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp).resolve()
        os.chdir(tmp_path)
        try:
            yield tmp_path
        finally:
            os.chdir(original)


def _read_alembic_revision(db_path: Path) -> str | None:
    """Read the alembic revision row directly from ``db_path``."""
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
    finally:
        engine.dispose()
    if row is None:
        return None
    return str(row[0])


# ---------------------------------------------------------------------------
# Tests — DB + pids/ + migrations
# ---------------------------------------------------------------------------


def test_init_creates_db_and_pids_dir() -> None:
    """``ctxr-fsm init --db <tmp>/fsm.db`` creates the project tree."""
    with tempfile.TemporaryDirectory() as tmp:
        project_dir = Path(tmp) / ".ctxr-fsm"
        db_path = project_dir / "fsm.db"

        result = runner.invoke(app, ["init", "--db", str(db_path)])

        assert result.exit_code == 0, result.output
        assert db_path.exists(), f"db not created at {db_path}"
        assert project_dir.is_dir()
        assert (project_dir / "pids").is_dir()


def test_init_runs_alembic_upgrade_head() -> None:
    """After init, the DB has a populated ``alembic_version`` row."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / ".ctxr-fsm" / "fsm.db"

        result = runner.invoke(app, ["init", "--db", str(db_path)])

        assert result.exit_code == 0, result.output
        revision = _read_alembic_revision(db_path)
        assert revision is not None
        assert revision != ""


def test_init_json_mode_emits_revision_and_paths() -> None:
    """``--json`` produces a parseable summary including the db / dirs."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / ".ctxr-fsm" / "fsm.db"

        result = runner.invoke(app, ["init", "--db", str(db_path), "--json"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        # Resolve both sides so symlinks (e.g. /var → /private/var on macOS)
        # don't trip up the string-equality assert.
        assert Path(payload["db_path"]).resolve() == db_path.resolve()
        assert Path(payload["project_dir"]).resolve() == db_path.parent.resolve()
        assert Path(payload["pids_dir"]).resolve() == (db_path.parent / "pids").resolve()
        assert payload["alembic_revision"]  # truthy, non-empty


# ---------------------------------------------------------------------------
# Tests — idempotence
# ---------------------------------------------------------------------------


def test_init_is_idempotent() -> None:
    """Running init twice does not error and leaves the DB intact."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / ".ctxr-fsm" / "fsm.db"

        first = runner.invoke(app, ["init", "--db", str(db_path)])
        assert first.exit_code == 0, first.output
        rev_first = _read_alembic_revision(db_path)

        second = runner.invoke(app, ["init", "--db", str(db_path)])
        assert second.exit_code == 0, second.output
        rev_second = _read_alembic_revision(db_path)

        assert rev_first == rev_second
        assert db_path.exists()
        assert (db_path.parent / "pids").is_dir()


# ---------------------------------------------------------------------------
# Tests — .gitignore handling
# ---------------------------------------------------------------------------


def test_init_appends_gitignore_entry_when_git_dir_present(cwd_tmpdir: Path) -> None:
    """A fake ``.git/`` triggers the ``.ctxr-fsm/`` ignore entry."""
    (cwd_tmpdir / ".git").mkdir()

    db_path = cwd_tmpdir / ".ctxr-fsm" / "fsm.db"
    result = runner.invoke(app, ["init", "--db", str(db_path)])
    assert result.exit_code == 0, result.output

    gitignore = cwd_tmpdir / ".gitignore"
    assert gitignore.is_file()
    contents = gitignore.read_text(encoding="utf-8").splitlines()
    assert ".ctxr-fsm/" in contents


def test_init_gitignore_idempotent_on_repeated_init(cwd_tmpdir: Path) -> None:
    """Re-running init does not duplicate the ``.ctxr-fsm/`` line."""
    (cwd_tmpdir / ".git").mkdir()

    db_path = cwd_tmpdir / ".ctxr-fsm" / "fsm.db"

    for _ in range(3):
        result = runner.invoke(app, ["init", "--db", str(db_path)])
        assert result.exit_code == 0, result.output

    gitignore = cwd_tmpdir / ".gitignore"
    contents = gitignore.read_text(encoding="utf-8").splitlines()
    matches = [line for line in contents if line.strip() == ".ctxr-fsm/"]
    assert len(matches) == 1, f"expected exactly one entry, got {matches!r}"


def test_init_preserves_existing_gitignore_content(cwd_tmpdir: Path) -> None:
    """Existing entries survive the append."""
    (cwd_tmpdir / ".git").mkdir()
    existing = "node_modules/\n__pycache__/\n"
    (cwd_tmpdir / ".gitignore").write_text(existing, encoding="utf-8")

    db_path = cwd_tmpdir / ".ctxr-fsm" / "fsm.db"
    result = runner.invoke(app, ["init", "--db", str(db_path)])
    assert result.exit_code == 0, result.output

    contents = (cwd_tmpdir / ".gitignore").read_text(encoding="utf-8")
    assert "node_modules/" in contents
    assert "__pycache__/" in contents
    assert ".ctxr-fsm/" in contents.splitlines()


def test_init_does_not_create_gitignore_without_git_dir(cwd_tmpdir: Path) -> None:
    """No ``.git/`` means no ``.gitignore`` should be created."""
    assert not (cwd_tmpdir / ".git").exists()

    db_path = cwd_tmpdir / ".ctxr-fsm" / "fsm.db"
    result = runner.invoke(app, ["init", "--db", str(db_path)])
    assert result.exit_code == 0, result.output

    assert not (cwd_tmpdir / ".gitignore").exists()


def test_init_json_reports_gitignore_updated_flag(cwd_tmpdir: Path) -> None:
    """First init flips ``gitignore_updated`` true; second leaves it false."""
    (cwd_tmpdir / ".git").mkdir()
    db_path = cwd_tmpdir / ".ctxr-fsm" / "fsm.db"

    first = runner.invoke(app, ["init", "--db", str(db_path), "--json"])
    assert first.exit_code == 0, first.output
    first_payload = json.loads(first.stdout)
    assert first_payload["gitignore_updated"] is True

    second = runner.invoke(app, ["init", "--db", str(db_path), "--json"])
    assert second.exit_code == 0, second.output
    second_payload = json.loads(second.stdout)
    assert second_payload["gitignore_updated"] is False
