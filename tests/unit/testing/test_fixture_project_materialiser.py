"""Unit tests for ``ctxr.fsm.testing.materialise_fixture_project``."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ctxr.fsm.testing import materialise_fixture_project


def test_materialiser_copies_expected_files(tmp_path: Path) -> None:
    dest = tmp_path / "fixture"
    root = materialise_fixture_project(dest)

    assert root == dest.resolve()
    assert (root / "package.json").is_file()
    assert (root / "tsconfig.json").is_file()
    assert (root / "src" / "bad_type.ts").is_file()
    assert (root / "src" / "bad_unused.ts").is_file()
    assert (root / ".gitignore").is_file()
    # README is for human readers of the template directory; should
    # NOT be copied into the materialised tree.
    assert not (root / "README.md").exists()
    # Template name should be renamed away.
    assert not (root / "gitignore.template").exists()


def test_materialiser_seeds_two_commits_with_diffable_head(tmp_path: Path) -> None:
    root = materialise_fixture_project(tmp_path / "fixture")
    log = subprocess.check_output(
        ["git", "-C", str(root), "log", "--oneline"], text=True
    ).splitlines()
    assert len(log) == 2, f"expected 2 commits, got {log!r}"

    # The HEAD~1..HEAD diff must be non-empty so skills that scan
    # changed paths see something to review.
    diff = subprocess.check_output(
        ["git", "-C", str(root), "diff", "--name-only", "HEAD~1", "HEAD"],
        text=True,
    ).splitlines()
    assert diff == ["src/bad_type.ts"]


def test_materialiser_refuses_existing_dest(tmp_path: Path) -> None:
    dest = tmp_path / "fixture"
    dest.mkdir()
    with pytest.raises(FileExistsError):
        materialise_fixture_project(dest)


def test_materialiser_refuses_missing_parent(tmp_path: Path) -> None:
    dest = tmp_path / "missing-parent" / "fixture"
    with pytest.raises(FileNotFoundError):
        materialise_fixture_project(dest)
