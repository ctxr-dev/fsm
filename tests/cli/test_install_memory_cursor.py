"""Tests for ``ctxr-fsm install-memory --client cursor``.

Cursor uses standalone rule files under ``.cursor/rules/``; the
adapter writes the package's ``principles.cursor.md`` verbatim to
``.cursor/rules/ctxr-fsm.mdc``. There is no marker block — version
tracking falls back to the YAML ``version:`` key inside the file's
frontmatter.

These tests verify:

* The rule file is created with the right frontmatter (Cursor's
  ``description:`` / ``globs:`` / ``alwaysApply:`` header).
* Two consecutive installs produce a byte-identical rule file.
* ``--check`` against a simulated package bump reports drift.
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


def _install_bumped_principles(
    monkeypatch: pytest.MonkeyPatch, scratch: Path, new_version: str
) -> None:
    """Patch ``get_principles_path`` to point at a bumped copy of every file."""
    scratch.mkdir(parents=True, exist_ok=True)
    for client, filename in _FILENAME_BY_CLIENT.items():
        original = get_principles_path(client).read_text(encoding="utf-8")
        bumped = original.replace("version: 0.1.0", f"version: {new_version}")
        (scratch / filename).write_text(bumped, encoding="utf-8")

    def fake_get(client: str = "claude") -> Path:
        return scratch / _FILENAME_BY_CLIENT[client]

    monkeypatch.setattr(install_memory_cmd, "get_principles_path", fake_get)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_install_creates_cursor_rule_with_frontmatter(tmp_target: Path) -> None:
    """``.cursor/rules/ctxr-fsm.mdc`` is created with the Cursor frontmatter."""
    (tmp_target / ".cursor" / "rules").mkdir(parents=True)

    result = runner.invoke(
        app,
        [
            "install-memory",
            "--target",
            str(tmp_target),
            "--client",
            "cursor",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    rule = tmp_target / ".cursor" / "rules" / "ctxr-fsm.mdc"
    assert rule.is_file()

    body = rule.read_text(encoding="utf-8")
    # First line opens the frontmatter block.
    assert body.startswith("---\n")
    # Cursor-specific keys must be present.
    assert "description: ctxr-fsm FSM-usage discipline" in body
    assert "globs:" in body
    assert "alwaysApply:" in body
    # And the canonical principles version block is also inside.
    assert "version: 0.1.0" in body
    assert "# ctxr-fsm: how an agent must use the FSM" in body


def test_install_is_byte_identical_on_second_run(tmp_target: Path) -> None:
    """Re-running install leaves the Cursor rule byte-for-byte unchanged."""
    (tmp_target / ".cursor" / "rules").mkdir(parents=True)
    rule = tmp_target / ".cursor" / "rules" / "ctxr-fsm.mdc"

    first = runner.invoke(
        app,
        [
            "install-memory",
            "--target",
            str(tmp_target),
            "--client",
            "cursor",
            "--json",
        ],
    )
    assert first.exit_code == 0, first.output
    after_first = rule.read_bytes()

    second = runner.invoke(
        app,
        [
            "install-memory",
            "--target",
            str(tmp_target),
            "--client",
            "cursor",
            "--json",
        ],
    )
    assert second.exit_code == 0, second.output
    after_second = rule.read_bytes()

    assert after_first == after_second
    payload = json.loads(second.stdout)
    assert payload["results"][0]["action"] == "noop"


def test_check_detects_out_of_date_after_version_bump(
    tmp_target: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bumped package version shows up as ``out-of-date`` for cursor."""
    (tmp_target / ".cursor" / "rules").mkdir(parents=True)

    install = runner.invoke(
        app,
        [
            "install-memory",
            "--target",
            str(tmp_target),
            "--client",
            "cursor",
            "--json",
        ],
    )
    assert install.exit_code == 0, install.output

    bumped_dir = tmp_target / "_bumped_principles"
    _install_bumped_principles(monkeypatch, bumped_dir, "3.1.4")

    check = runner.invoke(
        app,
        [
            "install-memory",
            "--target",
            str(tmp_target),
            "--client",
            "cursor",
            "--check",
            "--json",
        ],
    )
    assert check.exit_code == 1, check.output
    payload = json.loads(check.stdout)
    assert payload["package_version"] == "3.1.4"
    row = payload["results"][0]
    assert row["client"] == "cursor"
    assert row["installed_version"] == "0.1.0"
    assert row["status"] == "out-of-date"
