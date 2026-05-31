"""Tests for ``ctxr-fsm install-memory --client auto``.

``auto`` runs every per-client detector and applies the install only
to the clients whose signature is present in the target directory:

* ``CLAUDE.md`` (or ``.claude/CLAUDE.md``) -> Claude.
* ``AGENTS.md`` -> Codex.
* ``.cursor/rules/`` directory -> Cursor.

These tests verify the three interesting branches:

* Both ``CLAUDE.md`` and ``AGENTS.md`` present -> both get patched.
* Empty target dir -> command exits cleanly with "no client detected"
  rather than erroring.
* Only ``.cursor/rules/`` present -> only the Cursor rule file is
  written (CLAUDE.md / AGENTS.md are NOT created).
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ctxr.fsm.cli import app
from ctxr.fsm.memory import get_principles_path

runner = CliRunner()


def _current_principles_version() -> str:
    """Read the principles version actually shipped in the package."""

    canonical = get_principles_path("canonical").read_text(encoding="utf-8")
    for line in canonical.splitlines():
        stripped = line.strip()
        if stripped.startswith("version:"):
            return stripped.split(":", 1)[1].strip().strip('"').strip("'")
    raise AssertionError("principles.md missing version: frontmatter line")


@pytest.fixture
def tmp_target() -> Iterator[Path]:
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp).resolve()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_auto_patches_both_claude_and_codex_when_both_present(
    tmp_target: Path,
) -> None:
    """Both CLAUDE.md and AGENTS.md present -> both get a marker block."""
    claude = tmp_target / "CLAUDE.md"
    agents = tmp_target / "AGENTS.md"
    claude.write_text("", encoding="utf-8")
    agents.write_text("", encoding="utf-8")

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
    clients_touched = {row["client"] for row in payload["results"]}
    assert {"claude", "codex"}.issubset(clients_touched)
    # Cursor should NOT be in the result set (no .cursor/rules dir).
    assert "cursor" not in clients_touched

    # Both host files now carry a marker block at the package's
    # current principles version (read from the source of truth so
    # this assertion survives future bumps without an edit).
    current = _current_principles_version()
    assert f"<!-- ctxr-fsm:begin v={current} -->" in claude.read_text(encoding="utf-8")
    assert f"<!-- ctxr-fsm:begin v={current} -->" in agents.read_text(encoding="utf-8")

    # And Claude's @ import points at the materialised in-project file.
    assert "@.ctxr-fsm/memory/principles.claude.md" in claude.read_text(
        encoding="utf-8"
    )


def test_auto_in_empty_dir_falls_back_to_claude(tmp_target: Path) -> None:
    """W14k BLOCKER-3: auto in a COLD project bootstraps Claude as the fallback.

    The original "clean no-op" contract was a UX bug: it left
    SKILL.md's ``@.ctxr-fsm/memory/bootstrap.md`` reference dead in
    fresh projects because nothing ever staged the bootstrap doc.
    The fallback creates a minimal CLAUDE.md at the canonical location
    + stages principles.claude.md + bootstrap.md under
    ``.ctxr-fsm/memory/`` so the SKILL.md `@` import resolves.
    """
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
    # Claude is the fallback target so it appears in the results list
    # with action="wrote" (the first run that bootstraps the file).
    assert any(
        row["client"] == "claude" and row["action"] == "wrote"
        for row in payload["results"]
    ), f"expected claude 'wrote' action; got: {payload['results']!r}"

    # CLAUDE.md was bootstrapped at the canonical top-level location.
    assert (tmp_target / "CLAUDE.md").is_file()
    # Principles + bootstrap docs staged under .ctxr-fsm/memory/.
    assert (tmp_target / ".ctxr-fsm" / "memory" / "principles.claude.md").exists()
    assert (tmp_target / ".ctxr-fsm" / "memory" / "bootstrap.md").exists()
    # Codex + Cursor were NOT detected so their files don't appear.
    assert not (tmp_target / "AGENTS.md").exists()
    assert not (tmp_target / ".cursor").exists()


def test_auto_with_only_cursor_rules_writes_only_cursor(tmp_target: Path) -> None:
    """Only .cursor/rules/ present -> only the Cursor rule file is written."""
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
    clients_touched = {row["client"] for row in payload["results"]}
    assert clients_touched == {"cursor"}

    # The cursor rule file exists.
    assert (tmp_target / ".cursor" / "rules" / "ctxr-fsm.mdc").is_file()
    # No spurious CLAUDE.md / AGENTS.md created.
    assert not (tmp_target / "CLAUDE.md").exists()
    assert not (tmp_target / "AGENTS.md").exists()
