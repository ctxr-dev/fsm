"""Project-root resolution + sub-action wiring tests for ``run_ensure`` (W14b).

Fast in-process tests (no supervisor spawn) that nail down the
pipeline's no-side-effect branches: walk-up to the project root,
explicit ``--project-root`` override, and the per-action ``skipped``
defaults when every step is turned off.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ctxr.fsm.cli.ensure_cmd import _resolve_project_root, run_ensure


def test_resolves_explicit_project_root(tmp_path: Path) -> None:
    """``--project-root`` overrides the walk-up."""
    p = tmp_path / "x" / "y"
    p.mkdir(parents=True)
    assert _resolve_project_root(p) == p.resolve()


def test_walks_up_to_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Walk-up finds the nearest ancestor with ``.ctxr-fsm/``."""
    root = tmp_path / "proj"
    (root / ".ctxr-fsm").mkdir(parents=True)
    deep = root / "a" / "b" / "c"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)
    assert _resolve_project_root(None) == root.resolve()


def test_falls_back_to_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``.ctxr-fsm/`` in chain → cwd is the answer."""
    monkeypatch.chdir(tmp_path)
    assert _resolve_project_root(None) == tmp_path.resolve()


def test_ensure_with_all_steps_skipped_is_trivially_missing(
    tmp_path: Path,
) -> None:
    """Running --check with every step disabled produces a clean summary.

    Useful as the harness for testing the report shape itself.
    """
    summary = run_ensure(
        project_root=tmp_path,
        client="none",
        mode="full",
        no_memory=True,
        no_mcp_config=True,
        check=True,
        timeout=2.0,
    )
    # Steps disabled / not applicable should land as either
    # ``missing`` (init, supervisor) or ``skipped`` (memory,
    # mcp-config).
    assert summary["actions"]["memory"] == "skipped"
    assert summary["actions"]["mcp_config"] == "skipped"
    assert "project_root" in summary
    assert os.path.isabs(summary["project_root"])
    assert isinstance(summary["duration_ms"], int)
    assert summary["mcp_stdio_registered"] == []
    assert summary["subsystems"] == {}
