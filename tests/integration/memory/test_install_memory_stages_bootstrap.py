"""Integration tests for ``ctxr-fsm install-memory`` staging bootstrap.md.

W14e contract: every client install path must stage the canonical
bootstrap doc under ``<target>/.ctxr-fsm/memory/bootstrap.md``
alongside the principles file. This lets Principle 0's
``@.ctxr-fsm/memory/bootstrap.md`` reference inside ``principles.md``
resolve to a real file regardless of which AI-client hosts the
principles body (Claude follows the ``@`` transitively; Codex/Cursor
inline the principles and the reference is just text but the file
still must exist for the operator who follows the pointer).

These tests drive the public CLI surface end-to-end (Typer's
:class:`CliRunner` + tmpdir) and assert the staged file matches the
package source byte-for-byte.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ctxr.fsm.cli import app
from ctxr.fsm.memory import get_bootstrap_path

runner = CliRunner()


@pytest.fixture
def tmp_target() -> Iterator[Path]:
    """Yield a fresh tempdir to use as the install ``--target`` root."""

    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp).resolve()


def _staged_bootstrap_path(target: Path) -> Path:
    """Where bootstrap.md is staged inside the target after install."""

    return target / ".ctxr-fsm" / "memory" / "bootstrap.md"


def _staged_bytes(target: Path) -> bytes:
    """Read the staged bootstrap file's contents (resolves symlinks)."""

    staged = _staged_bootstrap_path(target)
    assert staged.exists(), f"bootstrap.md not staged at {staged}"
    # ``read_bytes`` follows symlinks, which is what we want — we
    # care about the resolved content matching the package source,
    # not whether staging happened via symlink or copy.
    return staged.read_bytes()


def _package_bootstrap_bytes() -> bytes:
    """The package's canonical bootstrap.md bytes (source of truth)."""

    return get_bootstrap_path().read_bytes()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_install_memory_for_claude_stages_bootstrap(tmp_target: Path) -> None:
    """``--client claude`` stages both principles.claude.md AND bootstrap.md."""

    (tmp_target / "CLAUDE.md").write_text("", encoding="utf-8")

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

    principles = tmp_target / ".ctxr-fsm" / "memory" / "principles.claude.md"
    assert principles.exists(), principles

    assert _staged_bytes(tmp_target) == _package_bootstrap_bytes()


def test_install_memory_for_codex_stages_bootstrap(tmp_target: Path) -> None:
    """``--client codex`` stages bootstrap.md even though principles are inlined.

    Codex inlines the principles body into ``AGENTS.md`` (no Claude
    ``@`` import), but Principle 0 inside that inlined body still
    points at ``@.ctxr-fsm/memory/bootstrap.md`` — the file must
    exist on disk so the operator who follows the pointer lands on
    real content.
    """

    (tmp_target / "AGENTS.md").write_text("", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "install-memory",
            "--target",
            str(tmp_target),
            "--client",
            "codex",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output

    # Codex itself does NOT stage a principles file under
    # .ctxr-fsm/memory/ (the principles body lives inlined in
    # AGENTS.md). Bootstrap, however, MUST be staged.
    assert _staged_bytes(tmp_target) == _package_bootstrap_bytes()


def test_install_memory_for_cursor_stages_bootstrap(tmp_target: Path) -> None:
    """``--client cursor`` stages bootstrap.md alongside the .mdc rule file."""

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

    assert (
        tmp_target / ".cursor" / "rules" / "ctxr-fsm.mdc"
    ).is_file()
    assert _staged_bytes(tmp_target) == _package_bootstrap_bytes()


def test_install_memory_auto_stages_bootstrap_when_any_client_present(
    tmp_target: Path,
) -> None:
    """``--client auto`` stages bootstrap.md once for the detected client set."""

    # Set up all three client signatures so ``auto`` fans out to each.
    (tmp_target / "CLAUDE.md").write_text("", encoding="utf-8")
    (tmp_target / "AGENTS.md").write_text("", encoding="utf-8")
    (tmp_target / ".cursor" / "rules").mkdir(parents=True)

    result = runner.invoke(
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
    assert result.exit_code == 0, result.output

    payload = json.loads(result.stdout)
    # Every per-client result row carries a non-None bootstrap_link_mode
    # — staging happened for each client, even though the file under
    # ``.ctxr-fsm/memory/`` is shared across them.
    for row in payload["results"]:
        assert row["bootstrap_link_mode"] in {"symlink", "copy"}, row

    assert _staged_bytes(tmp_target) == _package_bootstrap_bytes()


def test_install_memory_no_symlink_copies_bootstrap(tmp_target: Path) -> None:
    """``--no-symlink`` forces a real copy (not a symlink) of bootstrap.md."""

    (tmp_target / "CLAUDE.md").write_text("", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "install-memory",
            "--target",
            str(tmp_target),
            "--client",
            "claude",
            "--no-symlink",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output

    staged = _staged_bootstrap_path(tmp_target)
    assert staged.is_file()
    assert not staged.is_symlink(), "expected a real copy under --no-symlink"
    assert staged.read_bytes() == _package_bootstrap_bytes()

    payload = json.loads(result.stdout)
    assert payload["results"][0]["bootstrap_link_mode"] == "copy"
