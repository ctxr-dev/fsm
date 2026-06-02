"""Unit tests for ``_resolve_stdio_entry`` in install_mcp_cmd.

Cross-client bootstrap regression: writing the bare literal
``command="ctxr-fsm"`` into client configs made the registered MCP
server unreachable whenever the long-running client process did not
inherit the project venv's PATH (the most common cause: an operator
running ``uv run ctxr-fsm install-mcp`` — the venv is on PATH only
for that one ``uv run`` call). The resolver now emits either a
``uv run`` shape (when invoked from a uv-managed venv) or the
absolute resolved console-script path. These tests pin both branches
so future refactors cannot silently re-introduce the bare literal.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ctxr.fsm.cli.install_mcp_cmd import _resolve_stdio_entry


def test_resolve_stdio_entry_returns_uv_run_when_in_uv_managed_venv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Inside a uv-managed venv, the entry uses ``command="uv"``.

    We fake the layout by pointing ``VIRTUAL_ENV`` at a tmp directory
    that contains a python executable matching ``sys.executable``'s
    leaf name, then monkeypatching ``sys.executable`` to the faked
    path. ``shutil.which("uv")`` is forced to a real path so the
    branch fires regardless of whether ``uv`` is on the test
    runner's PATH.
    """
    fake_venv = tmp_path / ".venv"
    fake_bin = fake_venv / "bin"
    fake_bin.mkdir(parents=True)
    fake_python = fake_bin / "python"
    fake_python.write_text("")
    fake_python.chmod(0o755)

    monkeypatch.setenv("VIRTUAL_ENV", str(fake_venv))
    monkeypatch.setattr(sys, "executable", str(fake_python))
    monkeypatch.setattr(
        "ctxr.fsm.cli.install_mcp_cmd.shutil.which",
        lambda name: "/usr/local/bin/uv" if name == "uv" else None,
    )

    entry = _resolve_stdio_entry()
    assert entry["command"] == "uv"
    assert entry["args"] == ["run", "ctxr-fsm", "mcp", "--transport", "stdio"]


def test_resolve_stdio_entry_falls_back_to_absolute_argv0(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Outside a uv-managed venv, the entry carries the absolute argv[0].

    Bare ``"ctxr-fsm"`` is the regression we are guarding against —
    if a client config later writes a relative or unresolved path,
    the long-running client process has no way to spawn it. The
    fallback resolves ``sys.argv[0]`` to an absolute filesystem path
    so the persisted shape is unambiguous.
    """
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    fake_script = tmp_path / "ctxr-fsm"
    fake_script.write_text("#!/usr/bin/env python\n")
    fake_script.chmod(0o755)
    monkeypatch.setattr(sys, "argv", [str(fake_script)])

    entry = _resolve_stdio_entry()
    # Absolute path. Never the bare literal "ctxr-fsm".
    assert entry["command"] == str(fake_script.resolve())
    assert entry["command"] != "ctxr-fsm"
    assert entry["args"] == ["mcp", "--transport", "stdio"]


def test_resolve_stdio_entry_uses_shutil_which_when_argv0_is_bare_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pipx / global installs see ``sys.argv[0] == "ctxr-fsm"`` (no path).

    The previous resolver called ``Path("ctxr-fsm").resolve()`` which
    produced ``<cwd>/ctxr-fsm`` — an absolute path that doesn't exist
    on the operator's machine — and persisted that broken value into
    client configs (reintroducing the unreachable-MCP regression this
    file pins). The resolver must fall back to ``shutil.which`` when
    ``sys.argv[0]`` is not an existing file, so the discovered
    PATH-resolved absolute path is what lands in client configs.
    """
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    # Move cwd somewhere that DEFINITELY does not contain a file
    # named ``ctxr-fsm`` so the ``Path("ctxr-fsm").is_file()`` probe
    # cannot accidentally pass against a test-runner artefact.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["ctxr-fsm"])

    fake_installed = tmp_path / "opt" / "bin" / "ctxr-fsm"
    fake_installed.parent.mkdir(parents=True)
    fake_installed.write_text("#!/usr/bin/env python\n")
    fake_installed.chmod(0o755)

    monkeypatch.setattr(
        "ctxr.fsm.cli.install_mcp_cmd.shutil.which",
        lambda name: str(fake_installed) if name == "ctxr-fsm" else None,
    )

    entry = _resolve_stdio_entry()
    assert entry["command"] == str(fake_installed.resolve())
    # Crucially NOT the bare literal nor a fake cwd-resolved path.
    assert entry["command"] != "ctxr-fsm"
    assert entry["command"] != str((tmp_path / "ctxr-fsm").resolve())
    assert entry["args"] == ["mcp", "--transport", "stdio"]


def test_resolve_stdio_entry_raises_when_no_real_path_resolves(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If neither argv[0] nor PATH carries a real script, raise loudly.

    Silently writing an unreachable command into Claude / Codex /
    Cursor configs would reintroduce exactly the bug this resolver
    exists to prevent. A loud failure at install time is the only
    acceptable outcome.
    """
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["ctxr-fsm"])
    monkeypatch.setattr(
        "ctxr.fsm.cli.install_mcp_cmd.shutil.which",
        lambda name: None,
    )

    with pytest.raises(FileNotFoundError, match="ctxr-fsm console script"):
        _resolve_stdio_entry()
