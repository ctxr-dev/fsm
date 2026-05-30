"""Integration tests for ``--check`` detecting bootstrap.md drift.

W14e contract: ``ctxr-fsm install-memory --check`` MUST flag drift on
the staged ``.ctxr-fsm/memory/bootstrap.md`` copy in the same way it
flags drift on the principles file. Bootstrap.md ships without a
frontmatter version (it's a procedural doc, not a versioned policy),
so drift detection uses a content-hash comparison: any change to the
package source or the staged copy must produce a non-zero exit.

These tests verify two end-to-end flows:

* After install, mutate the staged bootstrap copy in place; ``--check``
  flags ``bootstrap_status: "out-of-date"`` and exits 1. Re-running
  install fixes the drift (the staged copy is re-materialised).
* Monkey-patch the package's bootstrap source to a different blob;
  ``--check`` flags drift the same way (the staged copy now differs
  from the new package content). Re-running install picks up the
  monkey-patched content.
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_check_flags_staged_bootstrap_mutation(tmp_target: Path) -> None:
    """Mutate the staged bootstrap copy; ``--check`` reports drift; reinstall fixes it."""

    # Set up Claude as the host (any client will do for this test).
    (tmp_target / "CLAUDE.md").write_text("", encoding="utf-8")
    _install_auto(tmp_target)

    staged = _staged_bootstrap(tmp_target)
    assert staged.is_file()

    # Baseline: a clean install reports ``bootstrap_status: "ok"`` for
    # every detected client.
    exit_code, payload = _run_check(tmp_target)
    assert exit_code == 0, payload
    for row in payload["results"]:
        assert row["bootstrap_status"] == "ok", row

    # Mutate the staged file (simulating accidental local edits or
    # tooling drift).
    staged.write_text("mutated\n", encoding="utf-8")

    exit_code, payload = _run_check(tmp_target)
    assert exit_code == 1, payload
    for row in payload["results"]:
        assert row["bootstrap_status"] == "out-of-date", row

    # Re-running install rewrites the staged copy back to the package
    # source.
    _install_auto(tmp_target)
    exit_code, payload = _run_check(tmp_target)
    assert exit_code == 0, payload
    for row in payload["results"]:
        assert row["bootstrap_status"] == "ok", row


def test_check_flags_package_bootstrap_bump(
    tmp_target: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bumped package bootstrap source flags drift on the staged copy."""

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
    # the patched "package" source — exit 1 + bootstrap_status flagged.
    exit_code, payload = _run_check(tmp_target)
    assert exit_code == 1, payload
    for row in payload["results"]:
        assert row["bootstrap_status"] == "out-of-date", row

    # Re-running install (with the monkey-patched source still in
    # effect) materialises the bumped content.
    _install_auto(tmp_target)
    exit_code, payload = _run_check(tmp_target)
    assert exit_code == 0, payload
    for row in payload["results"]:
        assert row["bootstrap_status"] == "ok", row
    assert _staged_bootstrap(tmp_target).read_text(encoding="utf-8").endswith(
        "<!-- v2 -->\n"
    )


def test_check_reports_not_installed_when_bootstrap_absent(
    tmp_target: Path,
) -> None:
    """If bootstrap.md was never staged, ``--check`` reports it as not-installed."""

    # Create only the principles host file; never run install. The
    # principles axis will be ``missing``, and the bootstrap axis
    # ``not-installed``. Either condition alone is enough to fail.
    (tmp_target / "CLAUDE.md").write_text("", encoding="utf-8")

    exit_code, payload = _run_check(tmp_target)
    assert exit_code == 1, payload
    for row in payload["results"]:
        assert row["bootstrap_status"] == "not-installed", row
