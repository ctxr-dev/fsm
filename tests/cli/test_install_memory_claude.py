"""Tests for ``ctxr-fsm install-memory --client claude``.

The Claude adapter wires the package's principles file into the
consumer project by:

1. Materialising ``./.ctxr-fsm/memory/principles.claude.md`` (symlink
   or copy) so the in-project file resolves.
2. Appending a marker-fenced block to ``CLAUDE.md`` that contains a
   single Claude ``@<path>`` import pointing at the materialised file.

These tests drive the public CLI surface through
:class:`typer.testing.CliRunner` against a fresh tempdir, exercising:

* The marker block lands with the expected ``@`` import line.
* Two consecutive runs produce byte-identical output (idempotence).
* ``--check`` after a simulated version bump in the package
  principles reports ``out-of-date``.
* ``--dry-run`` reports the patch but never writes the host file or
  materialises the in-project link.
* User content above the marker block is preserved verbatim across
  runs and across re-patches.
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


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


_FILENAME_BY_CLIENT: dict[str, str] = {
    "canonical": "principles.md",
    "claude": "principles.claude.md",
    "codex": "principles.codex.md",
    "cursor": "principles.cursor.md",
}


@pytest.fixture
def tmp_target() -> Iterator[Path]:
    """Yield a fresh tempdir to use as the install ``--target`` root."""
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp).resolve()


def _install_bumped_principles(
    monkeypatch: pytest.MonkeyPatch, scratch: Path, new_version: str
) -> None:
    """Patch ``get_principles_path`` to point at a bumped copy of every file.

    We copy each client's real principles file into ``scratch`` and
    rewrite the ``version:`` frontmatter key to ``new_version``. Then
    we monkeypatch the symbol the CLI module imported so the next
    ``--check`` run sees the bumped version as the "package" version.
    """
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


def test_install_patches_empty_claude_md_with_import_line(tmp_target: Path) -> None:
    """Empty CLAUDE.md gains a marker block containing the @ import."""
    host = tmp_target / "CLAUDE.md"
    host.write_text("", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "install-memory",
            "--target",
            str(tmp_target),
            "--client",
            "claude",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    body = host.read_text(encoding="utf-8")
    assert "<!-- ctxr-fsm:begin v=0.1.0 -->" in body
    assert "<!-- ctxr-fsm:end -->" in body
    assert "@.ctxr-fsm/memory/principles.claude.md" in body
    # The materialised in-project principles file (symlink or copy)
    # must also exist so the @ import resolves.
    linked = tmp_target / ".ctxr-fsm" / "memory" / "principles.claude.md"
    assert linked.exists()


def test_install_is_byte_identical_on_second_run(tmp_target: Path) -> None:
    """Re-running the install leaves CLAUDE.md byte-for-byte unchanged."""
    host = tmp_target / "CLAUDE.md"
    host.write_text("", encoding="utf-8")

    first = runner.invoke(
        app,
        [
            "install-memory",
            "--target",
            str(tmp_target),
            "--client",
            "claude",
            "--json",
        ],
    )
    assert first.exit_code == 0, first.output
    after_first = host.read_bytes()

    second = runner.invoke(
        app,
        [
            "install-memory",
            "--target",
            str(tmp_target),
            "--client",
            "claude",
            "--json",
        ],
    )
    assert second.exit_code == 0, second.output
    after_second = host.read_bytes()

    assert after_first == after_second, "second install must be idempotent"

    # And the second run's structured report records the noop action so
    # downstream callers can act on it.
    payload = json.loads(second.stdout)
    assert payload["results"][0]["action"] == "noop"


def test_check_reports_out_of_date_after_version_bump(
    tmp_target: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A simulated package version bump makes ``--check`` exit non-zero."""
    host = tmp_target / "CLAUDE.md"
    host.write_text("", encoding="utf-8")

    install = runner.invoke(
        app,
        [
            "install-memory",
            "--target",
            str(tmp_target),
            "--client",
            "claude",
            "--json",
        ],
    )
    assert install.exit_code == 0, install.output

    bumped_dir = tmp_target / "_bumped_principles"
    _install_bumped_principles(monkeypatch, bumped_dir, "9.9.9")

    check = runner.invoke(
        app,
        [
            "install-memory",
            "--target",
            str(tmp_target),
            "--client",
            "claude",
            "--check",
            "--json",
        ],
    )
    assert check.exit_code == 1, check.output
    payload = json.loads(check.stdout)
    assert payload["package_version"] == "9.9.9"
    row = payload["results"][0]
    assert row["client"] == "claude"
    assert row["installed_version"] == "0.1.0"
    assert row["status"] == "out-of-date"


def test_dry_run_prints_patch_but_does_not_write(tmp_target: Path) -> None:
    """``--dry-run`` describes the patch and leaves disk untouched."""
    host = tmp_target / "CLAUDE.md"
    host.write_text("# my notes\n", encoding="utf-8")
    original_bytes = host.read_bytes()

    result = runner.invoke(
        app,
        [
            "install-memory",
            "--target",
            str(tmp_target),
            "--client",
            "claude",
            "--dry-run",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True
    row = payload["results"][0]
    assert row["action"] == "dry-run"
    # The note should describe the patch shape.
    assert "would" in row["note"]

    # Disk untouched: host file bytes preserved AND the in-project
    # materialised link was never created.
    assert host.read_bytes() == original_bytes
    assert not (tmp_target / ".ctxr-fsm").exists()


def test_preserves_user_content_above_marker_block(tmp_target: Path) -> None:
    """User content above the marker block survives the patch."""
    host = tmp_target / "CLAUDE.md"
    user_content = (
        "# My project memory\n"
        "\n"
        "Some longstanding notes the human wrote.\n"
        "Another paragraph.\n"
    )
    host.write_text(user_content, encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "install-memory",
            "--target",
            str(tmp_target),
            "--client",
            "claude",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output

    body = host.read_text(encoding="utf-8")
    # Everything the user wrote is still there, in order, at the top.
    assert body.startswith(user_content.rstrip("\n"))
    assert "# My project memory" in body
    assert "Some longstanding notes the human wrote." in body
    assert "Another paragraph." in body
    # And the marker block sits AFTER the user content.
    block_start = body.index("<!-- ctxr-fsm:begin")
    notes_position = body.index("Another paragraph.")
    assert notes_position < block_start
