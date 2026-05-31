"""W14k BLOCKER-3: install-memory in a COLD project falls back to Claude.

Before this fix, a fresh project with no pre-existing CLAUDE.md /
AGENTS.md / .cursor/rules made ``ctxr-fsm ensure --client auto`` a
no-op for the memory step: every detector returned False so the
detections list was empty. Net effect: the SKILL.md
``@.ctxr-fsm/memory/bootstrap.md`` reference never resolved because
nothing ever staged bootstrap.md alongside principles.claude.md in the
project's state directory.

With the fix, the ``auto`` branch falls back to bootstrapping a Claude
host file at the canonical ``<target>/CLAUDE.md`` location when no
detector fires. The patcher creates the file; the bootstrap doc +
principles.claude.md land under ``.ctxr-fsm/memory/`` as they would
on a warm project; the SKILL.md reference resolves; the user is free
to rename / move the file afterward and re-running install-memory is
idempotent.
"""

from __future__ import annotations

from pathlib import Path

from ctxr.fsm.cli.install_memory_cmd import run_install_memory


def test_cold_project_auto_creates_claude_host_file(tmp_path: Path) -> None:
    """A fresh project (no CLAUDE.md / AGENTS.md / .cursor/rules) gets a Claude install."""
    # Fresh project — no client files present.
    assert not (tmp_path / "CLAUDE.md").exists()
    assert not (tmp_path / "AGENTS.md").exists()
    assert not (tmp_path / ".cursor" / "rules").exists()

    result = run_install_memory(target=tmp_path, client="auto")

    # The CLAUDE.md host file was created at the canonical top-level
    # location.
    assert (tmp_path / "CLAUDE.md").is_file(), (
        "expected CLAUDE.md to be bootstrapped in cold project"
    )

    # The principles + bootstrap docs were staged under
    # .ctxr-fsm/memory/ so the SKILL.md `@` reference resolves.
    state_dir = tmp_path / ".ctxr-fsm" / "memory"
    assert (state_dir / "principles.claude.md").exists()
    assert (state_dir / "bootstrap.md").exists()

    # The result envelope reports the install action so the operator
    # / agent can see what happened.
    assert "results" in result
    assert any(
        row["client"] == "claude" and row["action"] == "wrote"
        for row in result["results"]
    ), f"expected a claude 'wrote' action in results; got: {result['results']!r}"


def test_cold_project_auto_is_idempotent(tmp_path: Path) -> None:
    """Re-running install-memory on a freshly-bootstrapped cold project is a no-op."""
    # First call creates everything.
    run_install_memory(target=tmp_path, client="auto")
    claude_md_first = (tmp_path / "CLAUDE.md").read_bytes()
    principles_first = (
        tmp_path / ".ctxr-fsm" / "memory" / "principles.claude.md"
    ).read_bytes()

    # Second call must be a no-op (same bytes).
    second = run_install_memory(target=tmp_path, client="auto")
    claude_md_second = (tmp_path / "CLAUDE.md").read_bytes()
    principles_second = (
        tmp_path / ".ctxr-fsm" / "memory" / "principles.claude.md"
    ).read_bytes()

    assert claude_md_first == claude_md_second
    assert principles_first == principles_second
    assert any(
        row["client"] == "claude" and row["action"] == "noop"
        for row in second["results"]
    ), f"expected a claude 'noop' action on re-run; got: {second['results']!r}"


def test_warm_project_with_codex_still_picks_codex_only(tmp_path: Path) -> None:
    """The fallback ONLY activates when NO detector fires - a warm AGENTS.md project routes to codex normally."""
    (tmp_path / "AGENTS.md").write_text("existing codex memory\n", encoding="utf-8")

    result = run_install_memory(target=tmp_path, client="auto")

    # Codex was detected so the fallback didn't fire - no CLAUDE.md
    # was created.
    assert not (tmp_path / "CLAUDE.md").exists()
    assert (tmp_path / "AGENTS.md").is_file()
    assert any(
        row["client"] == "codex" for row in result["results"]
    ), f"expected codex in results; got: {result['results']!r}"
    assert not any(
        row["client"] == "claude" for row in result["results"]
    ), "claude fallback should not fire when codex was detected"
