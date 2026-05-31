"""Tests for ``ctxr-fsm install-memory --client codex``.

The Codex adapter targets ``AGENTS.md``. Codex does not support
Claude's ``@<path>`` import idiom so the principles content is INLINED
between the ctxr-fsm marker fences at the end of the file.

These tests verify:

* An empty ``AGENTS.md`` ends up with the full principles content
  inside the marker block (no separate import line).
* The patch is idempotent across two consecutive runs.
* ``--check`` against a simulated package version bump reports the
  drift and exits non-zero.
* The inlined body is byte-for-byte equal to the package's
  ``principles.codex.md`` (sans the trailing newline that the marker
  builder normalises).
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


def test_install_inlines_principles_into_empty_agents_md(tmp_target: Path) -> None:
    """Empty AGENTS.md ends up with the full principles inlined."""
    host = tmp_target / "AGENTS.md"
    host.write_text("", encoding="utf-8")

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
    body = host.read_text(encoding="utf-8")
    current = _current_principles_version()
    assert f"<!-- ctxr-fsm:begin v={current} -->" in body
    assert "<!-- ctxr-fsm:end -->" in body
    # No Claude-style fence-line @ import (that's a Claude-only idiom
    # for the principles file itself). The principles body IS inlined
    # below, and Principle 0 inside it does mention
    # ``@.ctxr-fsm/memory/bootstrap.md`` as prose — that's part of the
    # canonical content, not a Codex-incompatible import directive.
    assert "@.ctxr-fsm/memory/principles.codex.md" not in body
    # The principles body is inlined.
    assert "# ctxr-fsm: how an agent must use the FSM" in body
    assert "Principle 1: pre-check before you act" in body
    assert "Principle 10: subscribe for events when reasoning across states" in body


def test_install_is_byte_identical_on_second_run(tmp_target: Path) -> None:
    """Re-running install on AGENTS.md is a noop and byte-identical."""
    host = tmp_target / "AGENTS.md"
    host.write_text("", encoding="utf-8")

    first = runner.invoke(
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
    assert first.exit_code == 0, first.output
    after_first = host.read_bytes()

    second = runner.invoke(
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
    assert second.exit_code == 0, second.output
    after_second = host.read_bytes()

    assert after_first == after_second
    payload = json.loads(second.stdout)
    assert payload["results"][0]["action"] == "noop"


def test_check_detects_version_drift(
    tmp_target: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bumped package version shows up as ``out-of-date`` for codex."""
    host = tmp_target / "AGENTS.md"
    host.write_text("", encoding="utf-8")

    install = runner.invoke(
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
    assert install.exit_code == 0, install.output

    bumped_dir = tmp_target / "_bumped_principles"
    _install_bumped_principles(monkeypatch, bumped_dir, "2.0.0")

    check = runner.invoke(
        app,
        [
            "install-memory",
            "--target",
            str(tmp_target),
            "--client",
            "codex",
            "--check",
            "--json",
        ],
    )
    assert check.exit_code == 1, check.output
    payload = json.loads(check.stdout)
    assert payload["package_version"] == "2.0.0"
    row = payload["results"][0]
    assert row["client"] == "codex"
    assert row["installed_version"] == _current_principles_version()
    assert row["status"] == "out_of_date"


def test_inlined_content_matches_package_file_byte_for_byte(
    tmp_target: Path,
) -> None:
    """The body between the marker fences equals the package file's text."""
    host = tmp_target / "AGENTS.md"
    host.write_text("", encoding="utf-8")

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

    body = host.read_text(encoding="utf-8")
    current = _current_principles_version()
    begin = f"<!-- ctxr-fsm:begin v={current} -->\n"
    end = "\n<!-- ctxr-fsm:end -->"
    start_idx = body.index(begin) + len(begin)
    end_idx = body.index(end, start_idx)
    inlined_payload = body[start_idx:end_idx]

    package_text = get_principles_path("codex").read_text(encoding="utf-8")
    # The CLI normalises the payload via rstrip("\n") before inlining,
    # so compare against the stripped form.
    assert inlined_payload == package_text.rstrip("\n")
