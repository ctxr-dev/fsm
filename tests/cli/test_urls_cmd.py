"""W16: ``ctxr-fsm urls`` prints the subsystem table only (no diagnostic noise)."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from ctxr.fsm.cli import app

runner = CliRunner()


def _write_active_mcp(target: Path) -> None:
    """Drop a minimal active-mcp.json so urls has something to render."""
    state = target / ".ctxr-fsm"
    state.mkdir(parents=True, exist_ok=True)
    (state / "active-mcp.json").write_text(
        json.dumps(
            {
                "started_at": "2026-05-31T00:00:00.000+00:00",
                "supervisor_pid": 1111,
                "version": "0.2.0",
                "subsystems": {
                    "mcp": {
                        "http_url": "http://127.0.0.1:50001/sse",
                        "healthz_url": "http://127.0.0.1:50001/healthz",
                        "pid": 2222,
                    },
                    "api": {
                        "http_url": "http://127.0.0.1:50002",
                        "healthz_url": "http://127.0.0.1:50002/healthz",
                        "pid": 2223,
                        "docs_url": "http://127.0.0.1:50002/docs",
                    },
                    "ui": {
                        "http_url": "http://127.0.0.1:50003",
                        "healthz_url": None,
                        "pid": 2224,
                    },
                },
            }
        ),
        encoding="utf-8",
    )


def test_urls_prints_table_with_no_diagnostic_noise(tmp_path: Path) -> None:
    """urls outputs the table + URL block + nothing else (no DB panel, no alembic)."""
    _write_active_mcp(tmp_path)

    result = runner.invoke(app, ["urls", "--project-root", str(tmp_path)])

    assert result.exit_code == 0, result.output
    # Subsystem names appear in the table.
    assert "mcp" in result.output
    assert "api" in result.output
    assert "ui" in result.output
    # The URL block below the table carries the full URLs +
    # the "swagger" label for the api row's docs_url.
    assert "Open in your browser" in result.output
    assert "swagger" in result.output
    assert "http://127.0.0.1:50001/sse" in result.output
    assert "http://127.0.0.1:50002/docs" in result.output
    assert "http://127.0.0.1:50003" in result.output
    # No diagnostic noise from `doctor`.
    assert "Revision" not in result.output, "urls should not print the DB panel"
    assert "alembic" not in result.output.lower()


def test_urls_missing_supervisor_returns_actionable_error(tmp_path: Path) -> None:
    """When no active-mcp.json exists, urls exits non-zero with a hint."""
    result = runner.invoke(app, ["urls", "--project-root", str(tmp_path)])

    assert result.exit_code == 1
    assert "no supervisor running" in result.output.lower()
    assert "ctxr-fsm ensure" in result.output


def test_show_is_a_working_alias_for_urls(tmp_path: Path) -> None:
    """`show` reaches the same renderer as `urls`."""
    _write_active_mcp(tmp_path)
    result = runner.invoke(app, ["show", "--project-root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "mcp" in result.output
    assert "Open in your browser" in result.output
