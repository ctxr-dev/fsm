"""Integration tests for ``ctxr-fsm install-memory`` staging the W23-SSOT docs.

Contract (W23-SSOT):

* For **Claude**, the canonical reference docs that ship inside the
  ``ctxr-fsm`` package memory dir — ``AGENT_QUICKSTART.md``,
  ``SKILL_TEMPLATE.md``, ``GATE_CONTRACT.md`` — are staged under
  ``<target>/.ctxr-fsm/memory/<filename>.md`` alongside
  ``principles.claude.md`` and ``bootstrap.md`` so any sibling skill or
  agent in the workspace can resolve them via Claude's
  ``@.ctxr-fsm/memory/<filename>.md`` import syntax.
* For **Codex** and **Cursor**, the SSOT docs are NOT staged as
  separate files — those clients don't follow ``@`` imports, and
  unlike the bootstrap doc the SSOT docs are reference material rather
  than principles a single LLM session must internalise, so inlining
  them into the principles adapter would bloat that file without
  payoff. The install summary therefore reports
  ``ssot_link_modes: null`` for those clients.

These tests drive the public CLI surface end-to-end (Typer's
:class:`CliRunner` + tmpdir) and assert:

1. Fresh project: ``install-memory --client claude`` stages every SSOT
   doc under ``.ctxr-fsm/memory/`` and the staged bytes match the
   package source. The ``ssot_link_modes`` JSON entry lists every
   slug.
2. Idempotency: re-running ``install-memory`` against an already-staged
   target does not duplicate files, does not rewrite when content
   matches by hash, and the second run reports the same staging mode.
3. Drift detection: editing a staged SSOT doc in the consumer tree is
   flagged by ``install-memory --check`` with a non-zero exit and the
   per-slug ``ssot_statuses`` entry switches to ``out_of_date``.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ctxr.fsm.cli import app
from ctxr.fsm.memory import get_ssot_doc_path, list_ssot_doc_slugs

runner = CliRunner()


# The five files install-memory must stage under .ctxr-fsm/memory/
# after the Claude install path runs against a fresh project. Kept as
# a tuple of filenames so test failures point at exactly which file
# is missing rather than at an abstract "slug" set.
_EXPECTED_STAGED_FILES: tuple[str, ...] = (
    "principles.claude.md",
    "bootstrap.md",
    "AGENT_QUICKSTART.md",
    "SKILL_TEMPLATE.md",
    "GATE_CONTRACT.md",
)


@pytest.fixture
def tmp_target() -> Iterator[Path]:
    """Yield a fresh tempdir to use as the install ``--target`` root."""

    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp).resolve()


def _staged_memory_dir(target: Path) -> Path:
    return target / ".ctxr-fsm" / "memory"


def _install_claude(target: Path) -> dict:
    """Run ``install-memory --client claude --no-symlink`` and return the JSON payload.

    ``--no-symlink`` keeps the staged bytes mutable for the drift
    tests below (a symlink would update with the package source the
    moment we mutate that, which is not what the drift case models).
    """

    result = runner.invoke(
        app,
        [
            "install-memory",
            "--target",
            str(target),
            "--client",
            "claude",
            "--no-symlink",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def _run_check(target: Path) -> tuple[int, dict]:
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
    for row in payload["results"]:
        if row["client"] == client:
            return row
    raise AssertionError(f"no row for {client!r} in {payload}")


# ---------------------------------------------------------------------------
# Fresh-install staging
# ---------------------------------------------------------------------------


def test_install_memory_stages_all_five_files_on_fresh_claude_project(
    tmp_target: Path,
) -> None:
    """A fresh Claude install lands every SSOT + principles + bootstrap file."""

    (tmp_target / "CLAUDE.md").write_text("", encoding="utf-8")

    payload = _install_claude(tmp_target)

    memory_dir = _staged_memory_dir(tmp_target)
    for filename in _EXPECTED_STAGED_FILES:
        staged = memory_dir / filename
        assert staged.is_file(), f"expected staged file at {staged}"

    # The staged bytes for every SSOT doc match the package source.
    for slug in list_ssot_doc_slugs():
        package_path = get_ssot_doc_path(slug)
        staged = memory_dir / package_path.name
        assert staged.read_bytes() == package_path.read_bytes(), slug

    # The install summary surfaces ``ssot_link_modes`` for Claude.
    claude_row = _row_for(payload, "claude")
    assert claude_row["ssot_link_modes"] is not None
    assert set(claude_row["ssot_link_modes"]) == set(list_ssot_doc_slugs())
    # Every staged SSOT doc landed via ``copy`` (we passed --no-symlink).
    for slug, mode in claude_row["ssot_link_modes"].items():
        assert mode == "copy", (slug, mode)


def test_install_memory_does_not_stage_ssot_for_codex(tmp_target: Path) -> None:
    """Codex does not follow ``@`` imports, so SSOT docs stay un-staged."""

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

    memory_dir = _staged_memory_dir(tmp_target)
    for slug in list_ssot_doc_slugs():
        package_path = get_ssot_doc_path(slug)
        assert not (memory_dir / package_path.name).exists(), slug

    payload = json.loads(result.stdout)
    codex_row = _row_for(payload, "codex")
    assert codex_row["ssot_link_modes"] is None, codex_row


def test_install_memory_does_not_stage_ssot_for_cursor(tmp_target: Path) -> None:
    """Cursor does not follow ``@`` imports either."""

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

    memory_dir = _staged_memory_dir(tmp_target)
    for slug in list_ssot_doc_slugs():
        package_path = get_ssot_doc_path(slug)
        assert not (memory_dir / package_path.name).exists(), slug

    payload = json.loads(result.stdout)
    cursor_row = _row_for(payload, "cursor")
    assert cursor_row["ssot_link_modes"] is None, cursor_row


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_install_memory_is_idempotent_for_ssot_docs(tmp_target: Path) -> None:
    """Re-running install-memory does not duplicate or rewrite matching files."""

    (tmp_target / "CLAUDE.md").write_text("", encoding="utf-8")

    _install_claude(tmp_target)
    memory_dir = _staged_memory_dir(tmp_target)

    # Snapshot every staged file's bytes + mtime so we can detect any
    # rewrite or duplication on the second pass.
    snapshot: dict[str, tuple[bytes, float]] = {}
    for filename in _EXPECTED_STAGED_FILES:
        staged = memory_dir / filename
        snapshot[filename] = (staged.read_bytes(), staged.stat().st_mtime)

    # Run install-memory again. The host CLAUDE.md already imports the
    # principles file via the marker block, so the patch is a no-op;
    # the staged files all match the package source, so they should
    # not be rewritten either.
    second = _install_claude(tmp_target)

    # No new files in the memory dir.
    actual_files = sorted(p.name for p in memory_dir.iterdir())
    expected_files = sorted(_EXPECTED_STAGED_FILES)
    assert actual_files == expected_files, actual_files

    # The bytes match the snapshot (no content rewrite) AND the mtime is
    # unchanged (the staging path is a true no-op: we did not even
    # re-open + rewrite identical bytes).
    for filename in _EXPECTED_STAGED_FILES:
        staged = memory_dir / filename
        assert staged.read_bytes() == snapshot[filename][0], filename
        assert staged.stat().st_mtime == snapshot[filename][1], filename

    # ``--check`` reports a clean tree (no SSOT drift).
    exit_code, check_payload = _run_check(tmp_target)
    assert exit_code == 0, check_payload
    claude_row = _row_for(check_payload, "claude")
    for slug, status in claude_row["ssot_statuses"].items():
        assert status == "ok", (slug, status)

    # The second install reports the same staging mode for every slug.
    claude_row_second = _row_for(second, "claude")
    assert claude_row_second["ssot_link_modes"] is not None
    for slug, mode in claude_row_second["ssot_link_modes"].items():
        assert mode == "copy", (slug, mode)


# ---------------------------------------------------------------------------
# Drift detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug", list_ssot_doc_slugs())
def test_check_flags_mutated_ssot_doc_as_out_of_date(
    tmp_target: Path, slug: str
) -> None:
    """Editing a staged SSOT doc trips ``--check`` with a non-zero exit."""

    (tmp_target / "CLAUDE.md").write_text("", encoding="utf-8")
    _install_claude(tmp_target)

    package_path = get_ssot_doc_path(slug)
    staged = _staged_memory_dir(tmp_target) / package_path.name
    assert staged.is_file(), staged

    # Baseline: a clean install reports ``ok`` for every SSOT slug.
    exit_code, payload = _run_check(tmp_target)
    assert exit_code == 0, payload
    claude_row = _row_for(payload, "claude")
    assert claude_row["ssot_statuses"][slug] == "ok"

    # Mutate the staged file (simulating accidental local edits or
    # tooling drift).
    staged.write_text("mutated\n", encoding="utf-8")

    exit_code, payload = _run_check(tmp_target)
    assert exit_code == 1, payload
    claude_row = _row_for(payload, "claude")
    assert claude_row["ssot_statuses"][slug] == "out_of_date"

    # Other slugs stay clean.
    for other_slug, status in claude_row["ssot_statuses"].items():
        if other_slug == slug:
            continue
        assert status == "ok", (other_slug, status)

    # Re-running install rewrites the staged copy back to the package
    # source.
    _install_claude(tmp_target)
    exit_code, payload = _run_check(tmp_target)
    assert exit_code == 0, payload
    claude_row = _row_for(payload, "claude")
    assert claude_row["ssot_statuses"][slug] == "ok"


def test_check_flags_missing_ssot_doc_as_not_installed(tmp_target: Path) -> None:
    """Deleting a staged SSOT doc trips ``--check`` with ``not_installed``."""

    (tmp_target / "CLAUDE.md").write_text("", encoding="utf-8")
    _install_claude(tmp_target)

    # Remove one staged SSOT doc (pick the first slug for determinism).
    slug = list_ssot_doc_slugs()[0]
    staged = _staged_memory_dir(tmp_target) / get_ssot_doc_path(slug).name
    staged.unlink()

    exit_code, payload = _run_check(tmp_target)
    assert exit_code == 1, payload
    claude_row = _row_for(payload, "claude")
    assert claude_row["ssot_statuses"][slug] == "not_installed"


def test_check_reports_inlined_ssot_for_codex(tmp_target: Path) -> None:
    """Codex's SSOT axis is uniformly ``inlined`` (no staged files to hash)."""

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

    exit_code, payload = _run_check(tmp_target)
    # No SSOT drift on codex, but principles may be reported clean or
    # missing depending on the marker block — we only care about the
    # SSOT axis here.
    codex_row = _row_for(payload, "codex")
    for slug, status in codex_row["ssot_statuses"].items():
        assert status == "inlined", (slug, status)
    # Sanity: every known slug surfaces in the report.
    assert set(codex_row["ssot_statuses"]) == set(list_ssot_doc_slugs())
    # Exit code can be 0 (clean install) here — the principles block
    # was patched by the install above. The point of this test is the
    # SSOT axis, not the principles axis.
    assert exit_code in (0, 1), payload
