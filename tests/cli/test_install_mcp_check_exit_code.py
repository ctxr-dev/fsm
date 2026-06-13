"""Exit-code contract for ``ctxr-fsm install-mcp --check`` (issue #94).

The pure :func:`run_install_mcp` function reports per-client
``status`` correctly; the bug lived in the Typer command's exit-code
gate. After the W14i rename of the ``out-of-date`` wire value to
``out_of_date``, the ``--check`` exit logic still compared each
result's ``status`` against the old hyphenated literal, so a drifted
client produced ``status == "out_of_date"`` which was NOT in the stale
``("missing", "out-of-date")`` membership set. The command exited 0
even though a client had drifted, silently defeating any CI gate built
on the exit code.

These tests drive the full Typer command through ``CliRunner`` so the
exit code itself is asserted (the in-process ``run_install_mcp`` tests
in ``tests/integration/install_mcp/test_install_mcp_modes.py`` cover
the ``status`` field but never the exit code).
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from ctxr.fsm.cli import app

runner = CliRunner()


def _seed_workspace_marker(tmp_path: Path) -> None:
    """Make ``tmp_path`` look like a Claude Code workspace root."""
    (tmp_path / "CLAUDE.md").write_text("# project memory\n", encoding="utf-8")


def test_check_exits_nonzero_on_drifted_client(tmp_path: Path) -> None:
    """A pre-existing ctxr-fsm entry with stale args drives a non-zero exit.

    Regression guard for #94: the entry is present but its ``args``
    differ from the desired stdio shape, so the per-client status is
    ``out_of_date``. The command must surface that as exit 1 so CI can
    detect the drift.
    """
    _seed_workspace_marker(tmp_path)
    mcp_json = tmp_path / ".mcp.json"
    mcp_json.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "ctxr-fsm": {
                        "command": "ctxr-fsm",
                        "args": ["mcp"],  # missing --transport stdio -> drift
                        "env": {},
                    }
                }
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "install-mcp",
            "--target",
            str(tmp_path),
            "--client",
            "claude",
            "--check",
            "--json",
        ],
    )

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    claude_row = payload["results"][0]
    assert claude_row["status"] == "out_of_date", claude_row


def test_check_exits_nonzero_on_missing_client(tmp_path: Path) -> None:
    """A fresh workspace with no entry yet reports ``missing`` and exits 1."""
    _seed_workspace_marker(tmp_path)

    result = runner.invoke(
        app,
        [
            "install-mcp",
            "--target",
            str(tmp_path),
            "--client",
            "claude",
            "--check",
            "--json",
        ],
    )

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    claude_row = payload["results"][0]
    assert claude_row["status"] == "missing", claude_row


def test_check_exits_nonzero_on_failed_probe(tmp_path: Path) -> None:
    """A malformed existing config makes the probe ``failed`` and exits 1.

    When ``--check`` cannot even evaluate drift (here the existing
    ``.mcp.json`` is not valid JSON, so the merger raises and the result
    carries ``action == "failed"`` with no ``status``), the exit gate
    must still be non-zero. Otherwise a broken config would slip through
    a CI gate as exit 0 despite drift being indeterminate.
    """
    _seed_workspace_marker(tmp_path)
    mcp_json = tmp_path / ".mcp.json"
    mcp_json.write_text("{ this is not valid json", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "install-mcp",
            "--target",
            str(tmp_path),
            "--client",
            "claude",
            "--check",
            "--json",
        ],
    )

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    claude_row = payload["results"][0]
    assert claude_row["action"] == "failed", claude_row
    assert "status" not in claude_row, claude_row


def test_check_exits_zero_when_installed(tmp_path: Path) -> None:
    """After a real apply, ``--check`` reports ``installed`` and exits 0."""
    _seed_workspace_marker(tmp_path)

    apply = runner.invoke(
        app,
        ["install-mcp", "--target", str(tmp_path), "--client", "claude"],
    )
    assert apply.exit_code == 0, apply.output

    result = runner.invoke(
        app,
        [
            "install-mcp",
            "--target",
            str(tmp_path),
            "--client",
            "claude",
            "--check",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    claude_row = payload["results"][0]
    assert claude_row["status"] == "installed", claude_row
