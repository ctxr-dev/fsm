"""Unit tests for the sharded log-path convention.

Lives next to the other lifecycle-primitives tests. Guards the
"never flat-folder logs" rule: every persistent log file in
``.ctxr-fsm/`` MUST go through :func:`sharded_log_path` and land
under a YYYY/MM/DD nested tree so the directory can't accumulate
into one bottlenecked flat folder.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from ctxr.fsm.cli.lifecycle.primitives import sharded_log_path


def test_path_is_nested_under_yyyy_mm_dd(tmp_path: Path) -> None:
    """The returned path lives under ``logs/<category>/YYYY/MM/DD/`` exactly."""
    when = datetime(2026, 5, 30, 18, 14, 55, tzinfo=UTC)
    path = sharded_log_path("supervisor", project_root=tmp_path, when=when)

    expected = (
        tmp_path / ".ctxr-fsm" / "logs" / "supervisor" / "2026" / "05" / "30"
    )
    assert path.parent == expected
    assert path.name.startswith("supervisor-")
    assert path.suffix == ".log"


def test_parent_directory_is_created(tmp_path: Path) -> None:
    """The helper materialises the nested tree so the caller can open() immediately."""
    when = datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC)
    path = sharded_log_path("supervisor", project_root=tmp_path, when=when)
    assert path.parent.is_dir()


def test_filename_includes_hhmmss(tmp_path: Path) -> None:
    """The basename embeds the wall-clock HHMMSS so concurrent boots don't collide."""
    when = datetime(2026, 5, 30, 9, 7, 3, tzinfo=UTC)
    path = sharded_log_path("supervisor", project_root=tmp_path, when=when)
    assert path.name.startswith("supervisor-090703")


def test_pid_suffix_appended_when_supplied(tmp_path: Path) -> None:
    """A pid disambiguates concurrent writers within the same second."""
    when = datetime(2026, 5, 30, 18, 14, 55, tzinfo=UTC)
    path = sharded_log_path("supervisor", project_root=tmp_path, when=when, pid=42)
    assert path.name == "supervisor-181455-42.log"


def test_pid_suffix_absent_when_omitted(tmp_path: Path) -> None:
    """When the caller doesn't pass a pid, the suffix is just HHMMSS."""
    when = datetime(2026, 5, 30, 18, 14, 55, tzinfo=UTC)
    path = sharded_log_path("supervisor", project_root=tmp_path, when=when)
    assert path.name == "supervisor-181455.log"


def test_alternate_category_changes_top_level_subdir(tmp_path: Path) -> None:
    """A different category writes under its own subtree, not the supervisor one."""
    when = datetime(2026, 5, 30, 18, 14, 55, tzinfo=UTC)
    path = sharded_log_path("audit", project_root=tmp_path, when=when)
    assert "logs/audit/2026/05/30" in str(path)
    assert "logs/supervisor" not in str(path)


def test_extension_can_be_overridden(tmp_path: Path) -> None:
    """Callers that emit JSONL audit dumps can swap ``log`` for ``jsonl``."""
    when = datetime(2026, 5, 30, 18, 14, 55, tzinfo=UTC)
    path = sharded_log_path(
        "audit", project_root=tmp_path, when=when, extension="jsonl"
    )
    assert path.suffix == ".jsonl"


def test_default_when_uses_current_utc(tmp_path: Path) -> None:
    """Omitting ``when`` snapshots the current UTC moment for the shard."""
    before = datetime.now(tz=UTC)
    path = sharded_log_path("supervisor", project_root=tmp_path)
    after = datetime.now(tz=UTC)

    # The YYYY/MM/DD shard must be one of the two wall-clock days that
    # bracket the call (handles the once-per-decade UTC midnight race).
    matched = False
    for candidate in (before, after):
        expected_dir = (
            tmp_path
            / ".ctxr-fsm"
            / "logs"
            / "supervisor"
            / f"{candidate:%Y}"
            / f"{candidate:%m}"
            / f"{candidate:%d}"
        )
        if path.parent == expected_dir:
            matched = True
            break
    assert matched, f"path {path} doesn't match either {before} or {after}"


def test_file_itself_is_not_created(tmp_path: Path) -> None:
    """The helper materialises the parent dir; the caller writes the file."""
    when = datetime(2026, 5, 30, 18, 14, 55, tzinfo=UTC)
    path = sharded_log_path("supervisor", project_root=tmp_path, when=when)
    assert not path.exists()
