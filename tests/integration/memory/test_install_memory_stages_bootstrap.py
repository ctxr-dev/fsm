"""Integration tests for ``ctxr-fsm install-memory`` staging bootstrap.md.

W14e contract (updated):

* For **Claude**, the canonical bootstrap doc is staged under
  ``<target>/.ctxr-fsm/memory/bootstrap.md`` so Principle 0's
  ``@.ctxr-fsm/memory/bootstrap.md`` reference inside
  ``principles.md`` resolves to a real file. Claude's transitive
  ``@`` import is what makes the staged file reachable to the LLM.
* For **Codex** and **Cursor**, the bootstrap doc is NOT staged as a
  separate file. Instead, the bootstrap body is INLINED into Principle
  0 inside their principles adapter (see
  ``tools/generate_memory_adapters.py``) — that's the only way to
  deliver the content to an LLM that doesn't follow ``@`` imports.

These tests drive the public CLI surface end-to-end (Typer's
:class:`CliRunner` + tmpdir) and assert:

1. Claude's staged bootstrap.md matches the package source byte-for-byte.
2. Codex / Cursor do NOT stage bootstrap.md.
3. Codex's AGENTS.md marker block CONTAINS the inlined bootstrap fence
   markers and a recognisable bootstrap substring.
4. Cursor's .mdc rule file CONTAINS the inlined bootstrap fence
   markers and a recognisable bootstrap substring.
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

# The HTML markers the generator wraps around the inlined bootstrap
# block inside codex / cursor adapter files. Kept in the test as
# string literals rather than imported from the generator so a change
# to the marker text shows up here as a deliberate test edit.
_INLINE_BEGIN = (
    "<!-- bootstrap-content-begin "
    "(inlined for clients that don't follow @ imports) -->"
)
_INLINE_END = "<!-- bootstrap-content-end -->"

# A recognisable substring from bootstrap.md that survives heading
# demotion (the heading text itself is unchanged; only the leading
# ``#`` count grows). Used to verify the bootstrap BODY (not just
# the markers) landed inside the inlined block.
_BOOTSTRAP_SUBSTRING = "Step 1 — detect the package, then install ONCE if missing"


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


def test_install_memory_for_codex_does_not_stage_bootstrap(
    tmp_target: Path,
) -> None:
    """``--client codex`` does NOT stage bootstrap.md — it inlines it instead.

    Codex inlines the principles body (including the bootstrap block)
    into ``AGENTS.md``; the LLM cannot follow Claude's ``@`` import
    syntax, so a staged copy under ``.ctxr-fsm/memory/`` would be
    unreachable and is therefore not created.
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

    # Bootstrap.md must NOT be staged under .ctxr-fsm/memory/.
    assert not _staged_bootstrap_path(tmp_target).exists(), (
        "bootstrap.md should not be staged for codex (it is inlined "
        "into AGENTS.md instead)"
    )

    # The AGENTS.md marker block must contain the inlined bootstrap
    # block (begin marker, end marker, and a recognisable body
    # substring).
    agents_text = (tmp_target / "AGENTS.md").read_text(encoding="utf-8")
    assert _INLINE_BEGIN in agents_text, agents_text
    assert _INLINE_END in agents_text, agents_text
    assert _BOOTSTRAP_SUBSTRING in agents_text, agents_text

    # The JSON result must report ``bootstrap_link_mode: null`` for codex.
    payload = json.loads(result.stdout)
    assert payload["results"][0]["bootstrap_link_mode"] is None, payload


def test_install_memory_for_cursor_does_not_stage_bootstrap(
    tmp_target: Path,
) -> None:
    """``--client cursor`` does NOT stage bootstrap.md — it inlines it instead."""

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

    # Bootstrap.md must NOT be staged for cursor.
    assert not _staged_bootstrap_path(tmp_target).exists(), (
        "bootstrap.md should not be staged for cursor (it is inlined "
        "into the .mdc rule file instead)"
    )

    rule_text = (
        tmp_target / ".cursor" / "rules" / "ctxr-fsm.mdc"
    ).read_text(encoding="utf-8")
    assert _INLINE_BEGIN in rule_text, rule_text
    assert _INLINE_END in rule_text, rule_text
    assert _BOOTSTRAP_SUBSTRING in rule_text, rule_text

    payload = json.loads(result.stdout)
    assert payload["results"][0]["bootstrap_link_mode"] is None, payload


def test_install_memory_auto_stages_bootstrap_for_claude_only(
    tmp_target: Path,
) -> None:
    """``--client auto`` stages bootstrap.md only for Claude.

    With all three client signatures present, the auto fan-out must
    stage bootstrap.md exactly once (for Claude) and report
    ``bootstrap_link_mode`` as null for codex / cursor.
    """

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
    by_client = {row["client"]: row for row in payload["results"]}
    assert by_client["claude"]["bootstrap_link_mode"] in {"symlink", "copy"}, (
        by_client["claude"]
    )
    assert by_client["codex"]["bootstrap_link_mode"] is None, by_client["codex"]
    assert by_client["cursor"]["bootstrap_link_mode"] is None, by_client["cursor"]

    # The staged bootstrap (under Claude's path) matches the package source.
    assert _staged_bytes(tmp_target) == _package_bootstrap_bytes()


def test_install_memory_no_symlink_copies_bootstrap(tmp_target: Path) -> None:
    """``--no-symlink`` forces a real copy (not a symlink) of bootstrap.md.

    Applies to the Claude install path only (the only path that stages
    bootstrap.md).
    """

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
