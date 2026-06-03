"""Codex side of ``ctxr-fsm install-mcp`` (W14d).

Two paths to cover:

* **CLI preferred when present.** When the ``codex`` binary is on
  PATH, the installer invokes ``codex mcp add ...`` and does not
  touch the TOML directly. We monkeypatch the ``_can_use_codex_cli``
  probe + ``_invoke_codex_cli_add`` to assert dispatch.
* **TOML fallback when CLI absent.** When the probe returns False,
  the installer falls back to a direct TOML splice that preserves
  every other table.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ctxr.fsm.cli import install_mcp_cmd
from ctxr.fsm.cli.install_mcp_cmd import run_install_mcp


def _redirect_codex_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Point ``_codex_target`` at a tmpdir-rooted path.

    We monkeypatch the module's ``_codex_target`` helper rather than
    HOME so we don't accidentally pick up the host's real Codex
    config when ``_can_use_codex_cli`` returns True on the test
    machine.
    """
    target = tmp_path / "codex_home" / ".codex" / "config.toml"
    monkeypatch.setattr(install_mcp_cmd, "_codex_target", lambda: target)
    return target


def test_codex_cli_path_is_invoked_when_codex_binary_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If ``codex`` is on PATH, the installer dispatches to ``codex mcp add``.

    Asserts via a stub: the real subprocess would mutate the host's
    user-level config, which is unacceptable for a test. The stub
    records its invocation count + returns a synthetic "applied"
    outcome.
    """
    _redirect_codex_path(tmp_path, monkeypatch)
    monkeypatch.setattr(install_mcp_cmd, "_can_use_codex_cli", lambda: True)

    invocations: list[dict[str, bool]] = []

    def _stub_invoke(*, dry_run: bool) -> dict[str, str]:
        invocations.append({"dry_run": dry_run})
        return {
            "path": "~/.codex/config.toml",
            "action": "applied",
            "detail": "stubbed codex mcp add",
        }

    monkeypatch.setattr(install_mcp_cmd, "_invoke_codex_cli_add", _stub_invoke)

    proj = tmp_path / "proj"
    proj.mkdir()
    result = run_install_mcp(proj, client="codex")
    assert len(invocations) == 1
    assert invocations[0]["dry_run"] is False
    codex_row = next(r for r in result["results"] if r["client"] == "codex")
    assert codex_row["action"] == "applied"


def test_codex_direct_toml_when_codex_binary_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without ``codex`` on PATH, the installer hand-edits the TOML.

    Preserves every unrelated table verbatim; only adds (or replaces)
    the ``[mcp_servers.ctxr-fsm]`` block.
    """
    codex_path = _redirect_codex_path(tmp_path, monkeypatch)
    monkeypatch.setattr(install_mcp_cmd, "_can_use_codex_cli", lambda: False)

    # Seed an existing config with unrelated tables.
    codex_path.parent.mkdir(parents=True, exist_ok=True)
    original = (
        "[user]\n"
        'name = "test"\n'
        "\n"
        "[mcp_servers.linear]\n"
        'command = "uvx"\n'
        'args = ["mcp-linear"]\n'
        "\n"
        "[settings]\n"
        "auto_save = true\n"
    )
    codex_path.write_text(original, encoding="utf-8")

    proj = tmp_path / "proj"
    proj.mkdir()
    result = run_install_mcp(proj, client="codex")
    codex_row = next(r for r in result["results"] if r["client"] == "codex")
    assert codex_row["action"] == "applied", codex_row

    patched = codex_path.read_text(encoding="utf-8")
    # Other tables preserved (substring matches because we keep the
    # original whitespace + value lines).
    assert "[user]" in patched
    assert 'name = "test"' in patched
    assert "[mcp_servers.linear]" in patched
    assert "[settings]" in patched
    assert "auto_save = true" in patched
    # Our table is present. Compute the expected command shape from
    # the same helper the install path uses — that keeps the assertion
    # valid for both the uv-run shape and the absolute-path shape
    # Fix 4 introduced (rather than re-baking the bare-literal
    # regression).
    from ctxr.fsm.cli.install_mcp_cmd import _resolve_stdio_entry
    desired = _resolve_stdio_entry()
    assert "[mcp_servers.ctxr-fsm]" in patched
    assert f'command = "{desired["command"]}"' in patched
    assert '"mcp", "--transport", "stdio"' in patched


def test_codex_toml_unchanged_on_reapply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second TOML install when the block matches returns 'unchanged'.

    Mirrors the JSON merger's idempotency contract.
    """
    codex_path = _redirect_codex_path(tmp_path, monkeypatch)
    monkeypatch.setattr(install_mcp_cmd, "_can_use_codex_cli", lambda: False)

    proj = tmp_path / "proj"
    proj.mkdir()

    first = run_install_mcp(proj, client="codex")
    assert first["results"][0]["action"] == "applied"
    bytes_first = codex_path.read_bytes()

    second = run_install_mcp(proj, client="codex")
    assert second["results"][0]["action"] == "unchanged", second
    assert codex_path.read_bytes() == bytes_first
