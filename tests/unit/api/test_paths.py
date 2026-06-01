"""Unit coverage for :mod:`ctxr.fsm.api._paths`.

The :func:`looks_like_filesystem_db_path` predicate is the single
guard the API routes (`get_current_project` + `_build_doctor_report`)
use to decide whether to derive ``project_root`` / ``db_path_relative``
from ``engine.url.database``. The matrix below pins the contract
against every non-filesystem sentinel SQLAlchemy's SQLite dialect
can emit, enumerated by the W22a adversarial-verify workflow
``wbyobu5wr``. If a future dialect change widens the sentinel
surface, this test surfaces the regression before it can land a
``Path(':memory:').resolve()`` on the wire.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ctxr.fsm.api._paths import (
    looks_like_filesystem_db_path,
    project_root_and_relative,
)


# ---------------------------------------------------------------------------
# looks_like_filesystem_db_path — sentinel matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        None,  # sqlite://
        "",  # sqlite:///
        ":memory:",  # sqlite:///:memory:
        # SQLite URI-filename family — SQLAlchemy strips the query string
        # into url.query so the bare 'file:...' lands in url.database.
        "file::memory:",  # sqlite:///file::memory:?cache=shared&uri=true
        "file:test.db",  # sqlite:///file:test.db?mode=memory&uri=true
        "file:foo?mode=memory",  # if SQLAlchemy ever leaves the q in db
        "file:bar?mode=ro",  # URI-filename in read-only mode
        "FILE:UPPERCASE",  # case-insensitive prefix
    ],
)
def test_looks_like_filesystem_db_path_rejects_non_file_sentinels(
    value: str | None,
) -> None:
    """Every non-filesystem sentinel must return False so the route
    skips :func:`project_root_and_relative` derivation."""
    assert looks_like_filesystem_db_path(value) is False, (
        f"{value!r} is a non-filesystem sentinel but the guard accepted it; "
        "derivation would produce a misleading project_root value"
    )


@pytest.mark.parametrize(
    "value",
    [
        "fsm.db",  # bare filename
        ".ctxr-fsm/fsm.db",  # canonical layout, relative
        "/tmp/something/fsm.db",  # POSIX absolute
        "/Users/dev/work/.ctxr-fsm/fsm.db",  # real path with colon-free segments
        "C:\\Users\\dev\\fsm.db",  # Windows absolute (defensive)
        "./relative/path.db",
        "../sibling.db",
    ],
)
def test_looks_like_filesystem_db_path_accepts_real_paths(value: str) -> None:
    """Real filesystem paths — POSIX absolute, POSIX relative, Windows
    absolute, leading-dot relative — must pass the guard so the route
    can derive a meaningful relative form."""
    assert looks_like_filesystem_db_path(value) is True, (
        f"{value!r} is a real filesystem path but the guard rejected it"
    )


# ---------------------------------------------------------------------------
# project_root_and_relative — canonical and fallback layouts
# ---------------------------------------------------------------------------


def test_project_root_and_relative_canonical_layout(tmp_path: Path) -> None:
    """When the DB lives under ``<root>/.ctxr-fsm/fsm.db`` the helper
    returns the root and a ``.ctxr-fsm/fsm.db`` relative path — the
    portable form UI surfaces commit to shared configs."""
    project_root = tmp_path / "my-project"
    ctxr_dir = project_root / ".ctxr-fsm"
    ctxr_dir.mkdir(parents=True)
    db = ctxr_dir / "fsm.db"
    db.touch()

    root, rel = project_root_and_relative(str(db))
    assert root.resolve() == project_root.resolve()
    assert rel == ".ctxr-fsm/fsm.db"


def test_project_root_and_relative_non_canonical_layout(tmp_path: Path) -> None:
    """When the DB lives outside a ``.ctxr-fsm/`` ancestor (operator
    passed ad-hoc ``--db``), the relative path is just the filename
    and the root is the DB's parent directory — honest, not made up."""
    rogue_dir = tmp_path / "somewhere"
    rogue_dir.mkdir()
    db = rogue_dir / "random.db"
    db.touch()

    root, rel = project_root_and_relative(str(db))
    assert root.resolve() == rogue_dir.resolve()
    assert rel == "random.db"
