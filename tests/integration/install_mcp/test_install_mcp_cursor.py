"""Cursor side of ``ctxr-fsm install-mcp`` (W14d).

Cursor's MCP config lives at ``~/.cursor/mcp.json`` (user-level, not
per-project), so the test redirects ``HOME`` into a tmpdir via
``monkeypatch`` before invoking the merger.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ctxr.fsm.cli.install_mcp_cmd import _resolve_stdio_entry, run_install_mcp

# The expected entry is resolved AT IMPORT TIME from the same helper
# the install path uses, so this test stays valid whether the runner
# is in a uv-managed venv (``command="uv"`` + ``args=["run", ...]``)
# or a bare install (``command=<abs path>`` + ``args=[...]``). The
# previous hardcoded ``"ctxr-fsm"`` literal would silently re-bake the
# regression the fix is preventing.
EXPECTED_ENTRY = _resolve_stdio_entry()


def test_cursor_creates_user_config_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without any prior Cursor config, install writes ``~/.cursor/mcp.json``."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    # ``Path.home()`` uses HOME on POSIX; on macOS some Pythons also
    # respect ``USERPROFILE`` via expanduser. Set both for safety.
    monkeypatch.setenv("USERPROFILE", str(fake_home))

    target = tmp_path / "proj"
    target.mkdir()
    result = run_install_mcp(target, client="cursor")
    row = result["results"][0]
    assert row["client"] == "cursor"
    assert row["action"] == "applied", row

    cursor_path = fake_home / ".cursor" / "mcp.json"
    assert cursor_path.exists()
    payload = json.loads(cursor_path.read_text(encoding="utf-8"))
    assert payload == {"mcpServers": {"ctxr-fsm": EXPECTED_ENTRY}}


def test_cursor_preserves_other_servers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-existing Cursor config keeps its other servers intact."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))

    cursor_path = fake_home / ".cursor" / "mcp.json"
    cursor_path.parent.mkdir(parents=True, exist_ok=True)
    original = {
        "mcpServers": {
            "linear": {"command": "uvx", "args": ["mcp-linear"], "env": {}},
        }
    }
    cursor_path.write_text(json.dumps(original, indent=2) + "\n", encoding="utf-8")

    target = tmp_path / "proj"
    target.mkdir()
    result = run_install_mcp(target, client="cursor")
    assert result["results"][0]["action"] == "applied"

    patched = json.loads(cursor_path.read_text(encoding="utf-8"))
    assert patched["mcpServers"]["linear"] == original["mcpServers"]["linear"]
    assert patched["mcpServers"]["ctxr-fsm"] == EXPECTED_ENTRY
