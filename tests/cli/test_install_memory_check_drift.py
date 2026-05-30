"""Tests for ``ctxr-fsm install-memory --check`` drift detection.

The ``--check`` mode parses the version pinned in each client's
marker block (or, for Cursor, in the rule file's frontmatter), compares
it against the version inside the package's principles files, and
exits non-zero when anything is out of date or missing.

These tests verify the cross-client behaviour:

* After installing into Claude + Codex + Cursor and bumping the
  package version in-place (via a monkeypatched principles path), a
  single ``--check`` run reports drift for ALL three clients in one
  go.
* When no install has ever happened but client host files exist,
  ``--check`` reports each as ``missing`` (the marker / frontmatter
  version is absent) and exits non-zero — this is the "you haven't
  installed yet" signal.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ctxr.fsm.cli import app, install_memory_cmd
from ctxr.fsm.memory import get_principles_path

runner = CliRunner()


_FILENAME_BY_CLIENT: dict[str, str] = {
    "canonical": "principles.md",
    "claude": "principles.claude.md",
    "codex": "principles.codex.md",
    "cursor": "principles.cursor.md",
}


@pytest.fixture
def tmp_target() -> Iterator[Path]:
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp).resolve()


def _current_principles_version() -> str:
    """Read the principles version actually shipped in the package."""

    canonical = get_principles_path("canonical").read_text(encoding="utf-8")
    for line in canonical.splitlines():
        stripped = line.strip()
        if stripped.startswith("version:"):
            return stripped.split(":", 1)[1].strip().strip('"').strip("'")
    raise AssertionError("principles.md missing version: frontmatter line")


def _install_bumped_principles(
    monkeypatch: pytest.MonkeyPatch, scratch: Path, new_version: str
) -> None:
    """Patch ``get_principles_path`` to point at a bumped copy of every file."""
    scratch.mkdir(parents=True, exist_ok=True)
    current_version = _current_principles_version()
    for client, filename in _FILENAME_BY_CLIENT.items():
        original = get_principles_path(client).read_text(encoding="utf-8")
        bumped = original.replace(
            f"version: {current_version}", f"version: {new_version}"
        )
        (scratch / filename).write_text(bumped, encoding="utf-8")

    def fake_get(client: str = "claude") -> Path:
        return scratch / _FILENAME_BY_CLIENT[client]

    monkeypatch.setattr(install_memory_cmd, "get_principles_path", fake_get)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_check_reports_drift_across_all_clients_after_version_bump(
    tmp_target: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bumped package version flags drift for claude + codex + cursor."""
    # Set up all three client signatures so ``auto`` covers everything.
    (tmp_target / "CLAUDE.md").write_text("", encoding="utf-8")
    (tmp_target / "AGENTS.md").write_text("", encoding="utf-8")
    (tmp_target / ".cursor" / "rules").mkdir(parents=True)

    install = runner.invoke(
        app,
        [
            "install-memory",
            "--target",
            str(tmp_target),
            "--client",
            "auto",
            "--json",
        ],
    )
    assert install.exit_code == 0, install.output

    # Now simulate a package version bump (e.g. a release after the
    # currently-shipped version ships). We pick a sentinel that is
    # guaranteed to differ from whatever ``principles.md`` actually
    # ships as today, so the drift test stays meaningful across
    # future version bumps.
    bumped_version = "9.99.99"
    bumped_dir = tmp_target / "_bumped_principles"
    _install_bumped_principles(monkeypatch, bumped_dir, bumped_version)

    check = runner.invoke(
        app,
        [
            "install-memory",
            "--target",
            str(tmp_target),
            "--client",
            "auto",
            "--check",
            "--json",
        ],
    )
    assert check.exit_code == 1, check.output
    payload = json.loads(check.stdout)
    assert payload["package_version"] == bumped_version

    by_client = {row["client"]: row for row in payload["results"]}
    assert set(by_client) == {"claude", "codex", "cursor"}
    current = _current_principles_version()
    for client in ("claude", "codex", "cursor"):
        row = by_client[client]
        assert row["installed_version"] == current, row
        assert row["package_version"] == bumped_version, row
        assert row["status"] == "out-of-date", row


def test_check_before_any_install_reports_missing(tmp_target: Path) -> None:
    """Empty host files exist but no install has happened -> 'missing'."""
    # Create the host files but do not run install — there is no
    # marker block / version frontmatter yet.
    (tmp_target / "CLAUDE.md").write_text("", encoding="utf-8")
    (tmp_target / "AGENTS.md").write_text("", encoding="utf-8")
    (tmp_target / ".cursor" / "rules").mkdir(parents=True)
    # ``.cursor/rules/`` is a directory; ``--check`` walks the host
    # file (the .mdc) — when it isn't there yet the row is
    # 'not-installed'. We focus the assertion on the two file-based
    # clients (CLAUDE.md / AGENTS.md exist but are empty -> 'missing').

    result = runner.invoke(
        app,
        [
            "install-memory",
            "--target",
            str(tmp_target),
            "--client",
            "auto",
            "--check",
            "--json",
        ],
    )

    # Empty host files -> no marker block / no frontmatter version ->
    # 'missing' -> exit 1.
    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    by_client = {row["client"]: row for row in payload["results"]}

    # Claude / Codex host files exist but are empty: 'missing' (host
    # is present, version cannot be parsed).
    for client in ("claude", "codex"):
        assert by_client[client]["installed_version"] is None, by_client[client]
        assert by_client[client]["status"] == "missing", by_client[client]

    # Cursor: the rule file does not exist yet, so its host file is
    # absent -> 'not-installed'.
    assert by_client["cursor"]["installed_version"] is None
    assert by_client["cursor"]["status"] == "not-installed"
