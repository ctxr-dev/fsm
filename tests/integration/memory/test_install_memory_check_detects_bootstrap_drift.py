"""Integration tests for ``--check`` detecting bootstrap drift.

W14e contract (updated):

* For **Claude**, bootstrap.md is staged separately under
  ``.ctxr-fsm/memory/`` and drift is hash-detected directly on that
  staged file (``bootstrap_status: ok | out-of-date | not-installed``).
* For **Codex** and **Cursor**, the bootstrap content is inlined into
  the principles adapter file. There is no separate staged bootstrap
  to hash, so ``bootstrap_status`` is the static sentinel
  ``"inlined"`` for those clients. Drift in the bootstrap CONTENT for
  codex / cursor surfaces as principles-version drift: changing
  bootstrap.md and regenerating the adapters bumps the adapter file's
  bytes, which the existing principles version comparison catches.

These tests verify:

* Claude: mutating the staged bootstrap copy flags
  ``bootstrap_status: out-of-date``. Bumping the package source via
  monkeypatch also flags drift on Claude.
* Codex / Cursor: bootstrap drift surfaces via the principles
  adapter file diverging from the installed marker block (or the
  cursor rule file's bytes diverging from the package). We simulate
  by editing the installed AGENTS.md/.mdc marker block to a stale
  version and verify the principles axis flags it.
* Absent install (no client has been initialised): principles is
  ``missing`` for the host that exists; Claude's bootstrap_status is
  ``not-installed``; codex / cursor's bootstrap_status is
  ``inlined``.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ctxr.fsm.cli import app, install_memory_cmd
from ctxr.fsm.memory import get_bootstrap_path

runner = CliRunner()


@pytest.fixture
def tmp_target() -> Iterator[Path]:
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp).resolve()


def _staged_bootstrap(target: Path) -> Path:
    return target / ".ctxr-fsm" / "memory" / "bootstrap.md"


def _install_auto(target: Path) -> None:
    """Run ``install-memory --client auto`` against ``target``."""

    result = runner.invoke(
        app,
        [
            "install-memory",
            "--target",
            str(target),
            "--client",
            "auto",
            # Force copy so we can mutate the staged file in place
            # without affecting the package source.
            "--no-symlink",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output


def _run_check(target: Path) -> tuple[int, dict]:
    """Run ``install-memory --check`` and return ``(exit_code, parsed_json)``."""

    result = runner.invoke(
        app,
        [
            "install-memory",
            "--target",
            str(target),
            "--client",
            "auto",
            "--check",
            "--json",
        ],
    )
    return result.exit_code, json.loads(result.stdout)


def _row_for(payload: dict, client: str) -> dict:
    """Pull the per-client row out of a ``--check`` JSON payload."""

    for row in payload["results"]:
        if row["client"] == client:
            return row
    raise AssertionError(f"no row for {client!r} in {payload}")


# ---------------------------------------------------------------------------
# Claude-specific: staged-file drift
# ---------------------------------------------------------------------------


def test_check_flags_claude_staged_bootstrap_mutation(tmp_target: Path) -> None:
    """Mutate Claude's staged bootstrap copy; ``--check`` reports drift; reinstall fixes it."""

    (tmp_target / "CLAUDE.md").write_text("", encoding="utf-8")
    _install_auto(tmp_target)

    staged = _staged_bootstrap(tmp_target)
    assert staged.is_file()

    # Baseline: a clean install reports ``bootstrap_status: "ok"``.
    exit_code, payload = _run_check(tmp_target)
    assert exit_code == 0, payload
    assert _row_for(payload, "claude")["bootstrap_status"] == "ok"

    # Mutate the staged file (simulating accidental local edits or
    # tooling drift).
    staged.write_text("mutated\n", encoding="utf-8")

    exit_code, payload = _run_check(tmp_target)
    assert exit_code == 1, payload
    assert _row_for(payload, "claude")["bootstrap_status"] == "out-of-date"

    # Re-running install rewrites the staged copy back to the package
    # source.
    _install_auto(tmp_target)
    exit_code, payload = _run_check(tmp_target)
    assert exit_code == 0, payload
    assert _row_for(payload, "claude")["bootstrap_status"] == "ok"


def test_check_flags_claude_package_bootstrap_bump(
    tmp_target: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bumped package bootstrap source flags drift on Claude's staged copy."""

    (tmp_target / "CLAUDE.md").write_text("", encoding="utf-8")
    _install_auto(tmp_target)

    # Bake a "new package release" into a sibling tmpfile that the
    # installer's helpers will see as the canonical bootstrap source.
    bumped_dir = tmp_target / "_bumped_package_bootstrap"
    bumped_dir.mkdir()
    bumped_source = bumped_dir / "bootstrap.md"
    bumped_source.write_text(
        get_bootstrap_path().read_text(encoding="utf-8") + "\n<!-- v2 -->\n",
        encoding="utf-8",
    )

    def fake_get_bootstrap_path() -> Path:
        return bumped_source

    monkeypatch.setattr(
        install_memory_cmd, "get_bootstrap_path", fake_get_bootstrap_path
    )

    # The staged copy (made from the REAL package source) now lags
    # the patched "package" source — exit 1 + bootstrap_status flagged
    # on the Claude row.
    exit_code, payload = _run_check(tmp_target)
    assert exit_code == 1, payload
    assert _row_for(payload, "claude")["bootstrap_status"] == "out-of-date"

    # Re-running install (with the monkey-patched source still in
    # effect) materialises the bumped content.
    _install_auto(tmp_target)
    exit_code, payload = _run_check(tmp_target)
    assert exit_code == 0, payload
    assert _row_for(payload, "claude")["bootstrap_status"] == "ok"
    assert _staged_bootstrap(tmp_target).read_text(encoding="utf-8").endswith(
        "<!-- v2 -->\n"
    )


# ---------------------------------------------------------------------------
# Codex / Cursor: bootstrap_status is always "inlined"
# ---------------------------------------------------------------------------


def test_check_reports_inlined_for_codex_and_cursor(tmp_target: Path) -> None:
    """A clean codex / cursor install reports ``bootstrap_status: "inlined"``."""

    (tmp_target / "CLAUDE.md").write_text("", encoding="utf-8")
    (tmp_target / "AGENTS.md").write_text("", encoding="utf-8")
    (tmp_target / ".cursor" / "rules").mkdir(parents=True)
    _install_auto(tmp_target)

    exit_code, payload = _run_check(tmp_target)
    assert exit_code == 0, payload
    assert _row_for(payload, "codex")["bootstrap_status"] == "inlined"
    assert _row_for(payload, "cursor")["bootstrap_status"] == "inlined"


def test_check_flags_codex_principles_drift_when_adapter_bumps(
    tmp_target: Path,
) -> None:
    """Editing AGENTS.md's marker version flags principles-axis drift for codex.

    For codex, bootstrap content drift surfaces as principles-version
    drift (since the adapter regeneration bumps the file when
    bootstrap.md changes). We simulate "the adapter was bumped but
    the install is stale" by hand-editing the version in AGENTS.md's
    marker block and asserting ``status: out-of-date``.
    """

    (tmp_target / "AGENTS.md").write_text("", encoding="utf-8")
    _install_auto(tmp_target)

    agents = tmp_target / "AGENTS.md"
    original = agents.read_text(encoding="utf-8")
    # Replace the pinned version with a clearly-stale one. The marker
    # line looks like ``<!-- ctxr-fsm:begin v=0.2.0 -->``; we don't
    # rely on the exact version string in the test, just on the
    # ``v=`` attribute.
    bumped = original.replace(
        "<!-- ctxr-fsm:begin v=", "<!-- ctxr-fsm:begin v=0.0.0-stale-x-", 1
    )
    assert bumped != original, "marker substitution did not match"
    agents.write_text(bumped, encoding="utf-8")

    exit_code, payload = _run_check(tmp_target)
    assert exit_code == 1, payload
    assert _row_for(payload, "codex")["status"] == "out-of-date"
    # bootstrap_status stays ``"inlined"`` — for codex it's the
    # principles axis that catches the drift.
    assert _row_for(payload, "codex")["bootstrap_status"] == "inlined"


def test_check_flags_cursor_principles_drift_when_rule_file_bumps(
    tmp_target: Path,
) -> None:
    """Mutating the cursor .mdc rule file flags principles-axis drift for cursor.

    Cursor's rule file IS the principles adapter (no marker block to
    parse — frontmatter version), so editing it to bump the version
    string drifts the principles axis. Bootstrap content drift would
    show up the same way (regenerated adapter -> bumped bytes).
    """

    (tmp_target / ".cursor" / "rules").mkdir(parents=True)
    _install_auto(tmp_target)

    rule = tmp_target / ".cursor" / "rules" / "ctxr-fsm.mdc"
    original = rule.read_text(encoding="utf-8")
    bumped = original.replace("version: 0.2.0", "version: 0.0.0-stale", 1)
    assert bumped != original, "version substitution did not match"
    rule.write_text(bumped, encoding="utf-8")

    exit_code, payload = _run_check(tmp_target)
    assert exit_code == 1, payload
    assert _row_for(payload, "cursor")["status"] == "out-of-date"
    assert _row_for(payload, "cursor")["bootstrap_status"] == "inlined"


# ---------------------------------------------------------------------------
# Absent install
# ---------------------------------------------------------------------------


def test_check_reports_not_installed_when_bootstrap_absent_for_claude(
    tmp_target: Path,
) -> None:
    """No install: Claude flags principles ``missing`` AND bootstrap ``not-installed``."""

    (tmp_target / "CLAUDE.md").write_text("", encoding="utf-8")

    exit_code, payload = _run_check(tmp_target)
    assert exit_code == 1, payload
    claude = _row_for(payload, "claude")
    assert claude["bootstrap_status"] == "not-installed"


def test_check_reports_inlined_for_codex_when_principles_missing(
    tmp_target: Path,
) -> None:
    """No install: codex flags principles ``missing`` but bootstrap_status is ``inlined``.

    The bootstrap content for codex always rides inside the
    principles adapter (or is absent entirely along with it). We
    never report ``not-installed`` for a code path that has nothing
    to install — that would be a misleading "you're missing the
    bootstrap file" message when the real issue is principles aren't
    installed at all.
    """

    (tmp_target / "AGENTS.md").write_text("", encoding="utf-8")

    exit_code, payload = _run_check(tmp_target)
    assert exit_code == 1, payload
    codex = _row_for(payload, "codex")
    assert codex["status"] == "missing"
    assert codex["bootstrap_status"] == "inlined"
