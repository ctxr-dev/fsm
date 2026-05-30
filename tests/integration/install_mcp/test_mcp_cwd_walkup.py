"""Coverage for the cwd walk-up resolution in W14d.

``ctxr-fsm install-mcp`` writes a stdio MCP entry that runs
``ctxr-fsm mcp --transport stdio`` with no ``--db`` flag. The MCP
server must therefore discover its project DB by walking up from the
spawning client's cwd looking for ``.ctxr-fsm/``. Both
:func:`ctxr.fsm.mcp.server.resolve_db_path` and
:func:`ctxr.fsm.cli._common.resolve_db_path` implement that walk-up
in lock-step so the stdio entry stays portable across projects.

We exercise the pure resolver against a constructed tmpdir tree
(``project_root/.ctxr-fsm/`` + a deep subdir as the cwd) so the test
is fast and never spawns a subprocess.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ctxr.fsm.cli._common import resolve_db_path as cli_resolve
from ctxr.fsm.mcp.server import resolve_db_path as mcp_resolve


def _stage_project(tmp_path: Path) -> tuple[Path, Path]:
    """Create ``<root>/.ctxr-fsm`` and a deep nested subdir.

    Returns ``(project_root, deep_subdir)`` so the caller can chdir
    into ``deep_subdir`` and assert the walk-up finds ``project_root``.
    """
    project_root = tmp_path / "myproj"
    (project_root / ".ctxr-fsm").mkdir(parents=True)
    deep = project_root / "src" / "feature" / "module"
    deep.mkdir(parents=True)
    return project_root, deep


def test_cli_resolver_walks_up_to_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI resolver finds the nearest ancestor ``.ctxr-fsm/`` from cwd."""
    project_root, deep = _stage_project(tmp_path)
    monkeypatch.chdir(deep)
    monkeypatch.delenv("CTXR_FSM_DB", raising=False)

    resolved = cli_resolve(None)
    expected = (project_root / ".ctxr-fsm" / "fsm.db").resolve()
    assert resolved == expected


def test_mcp_resolver_walks_up_to_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP server resolver matches the CLI resolver exactly."""
    project_root, deep = _stage_project(tmp_path)
    monkeypatch.chdir(deep)
    monkeypatch.delenv("CTXR_FSM_DB", raising=False)

    resolved = mcp_resolve(None)
    expected = (project_root / ".ctxr-fsm" / "fsm.db").resolve()
    assert resolved == expected


def test_resolvers_agree_when_no_state_dir_in_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When no ancestor has ``.ctxr-fsm/``, fall back to cwd's default.

    The error case ("init has not been run") surfaces downstream when
    the caller tries to open the missing DB file; the resolver itself
    just hands back the canonical path.
    """
    isolated = tmp_path / "no_state_anywhere" / "deep"
    isolated.mkdir(parents=True)
    monkeypatch.chdir(isolated)
    monkeypatch.delenv("CTXR_FSM_DB", raising=False)

    expected = (isolated / ".ctxr-fsm" / "fsm.db").resolve()
    assert cli_resolve(None) == expected
    assert mcp_resolve(None) == expected


def test_explicit_db_flag_short_circuits_walkup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit ``--db`` argument takes precedence over the walk-up."""
    _project_root, deep = _stage_project(tmp_path)
    monkeypatch.chdir(deep)
    explicit = tmp_path / "elsewhere" / "custom.db"
    explicit.parent.mkdir(parents=True, exist_ok=True)

    assert cli_resolve(explicit) == explicit.resolve()
    assert mcp_resolve(explicit) == explicit.resolve()


def test_env_var_takes_precedence_over_walkup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``$CTXR_FSM_DB`` wins over the walk-up tier."""
    _project_root, deep = _stage_project(tmp_path)
    monkeypatch.chdir(deep)
    env_target = tmp_path / "via-env" / "fsm.db"
    env_target.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CTXR_FSM_DB", str(env_target))

    expected = env_target.resolve()
    assert cli_resolve(None) == expected
    assert mcp_resolve(None) == expected


def test_immediate_cwd_with_state_dir_is_picked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When cwd itself has ``.ctxr-fsm``, no actual walking is needed."""
    project_root = tmp_path / "topproj"
    (project_root / ".ctxr-fsm").mkdir(parents=True)
    monkeypatch.chdir(project_root)
    monkeypatch.delenv("CTXR_FSM_DB", raising=False)

    expected = (project_root / ".ctxr-fsm" / "fsm.db").resolve()
    assert cli_resolve(None) == expected
    assert mcp_resolve(None) == expected
