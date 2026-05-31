"""W16: ``urls`` re-probes healthz live, not the stale boot snapshot.

Regression for the operator-visible bug where ``ctxr-fsm urls``
displayed every subsystem as ``unknown`` after a successful ``ensure``
because it trusted ``active-mcp.json``'s boot-time payload (which has
no ``status`` field) without re-probing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from ctxr.fsm.cli import app
from ctxr.fsm.cli.urls_cmd import (
    _augment_active_with_live_status,
    _live_status_for_subsystem,
)

runner = CliRunner()


def _stage_active(target: Path, subsystems: dict[str, dict[str, Any]]) -> None:
    state = target / ".ctxr-fsm"
    state.mkdir(parents=True, exist_ok=True)
    (state / "active-mcp.json").write_text(
        json.dumps(
            {
                "started_at": "2026-05-31T00:00:00.000+00:00",
                "supervisor_pid": 1111,
                "version": "0.2.0",
                "subsystems": subsystems,
            }
        ),
        encoding="utf-8",
    )


def test_live_status_returns_missing_for_dead_pid() -> None:
    """A pid that no longer exists -> ``missing`` (so the table colours red)."""
    block = {
        "http_url": "http://127.0.0.1:51234/sse",
        "healthz_url": "http://127.0.0.1:51234/healthz",
        "pid": 99999999,  # absurdly high pid, almost certainly not alive
    }
    assert _live_status_for_subsystem(block) == "missing"


def test_live_status_returns_unreachable_for_alive_pid_with_dead_healthz(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Alive pid but healthz refuses / 5xx -> ``unreachable``."""
    monkeypatch.setattr("ctxr.fsm.cli.urls_cmd.pid_is_alive", lambda _pid: True)
    block = {
        "http_url": "http://127.0.0.1:1/sse",  # port 1 will refuse
        "healthz_url": "http://127.0.0.1:1/healthz",
        "pid": 1,
    }
    assert _live_status_for_subsystem(block) == "unreachable"


def test_live_status_no_healthz_url_falls_back_to_pid_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UI-style subsystem with healthz_url=None falls back to live-pid only."""
    monkeypatch.setattr("ctxr.fsm.cli.urls_cmd.pid_is_alive", lambda _pid: True)
    block = {
        "http_url": "http://127.0.0.1:5173",
        "healthz_url": None,
        "pid": 1234,
    }
    assert _live_status_for_subsystem(block) == "ready"


def test_augment_injects_live_status_per_subsystem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The augment helper replaces (or adds) ``status`` on every dict subsystem."""
    monkeypatch.setattr(
        "ctxr.fsm.cli.urls_cmd._live_status_for_subsystem",
        lambda block: "ready" if block.get("pid") else "missing",
    )
    payload = {
        "subsystems": {
            "mcp": {"pid": 1001, "http_url": "x"},
            "api": {"pid": None, "http_url": "y"},
        }
    }
    out = _augment_active_with_live_status(payload)
    assert out["subsystems"]["mcp"]["status"] == "ready"
    assert out["subsystems"]["api"]["status"] == "missing"


def test_urls_cli_does_not_report_unknown_when_subsystems_alive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: when the staged subsystems probe live, the table NEVER says ``unknown``.

    This is the regression that motivated the live-probe: ``urls``
    previously showed ``unknown`` even right after a successful
    ``ensure`` because the discovery file had no status field.
    """
    _stage_active(
        tmp_path,
        {
            "mcp": {
                "http_url": "http://127.0.0.1:50001/sse",
                "healthz_url": "http://127.0.0.1:50001/healthz",
                "pid": 50001,
            },
            "api": {
                "http_url": "http://127.0.0.1:50002",
                "healthz_url": "http://127.0.0.1:50002/healthz",
                "pid": 50002,
                "docs_url": "http://127.0.0.1:50002/docs",
            },
        },
    )
    # Force the live probe to ALWAYS report ``ready`` for the test —
    # we are pinning the wiring, not the network state.
    monkeypatch.setattr(
        "ctxr.fsm.cli.urls_cmd._live_status_for_subsystem",
        lambda _block: "ready",
    )

    result = runner.invoke(app, ["urls", "--project-root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "unknown" not in result.output, (
        f"urls must not display ``unknown`` when the live probe reports "
        f"``ready``; got:\n{result.output}"
    )
    assert "ready" in result.output


def test_urls_cli_displays_missing_when_pid_dead(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the live probe reports ``missing`` (dead pid), the table reflects it.

    Without the live-probe wiring, a stopped supervisor would still
    display its boot-time URLs as ``unknown`` (the colour mapping's
    safe fallback) — operators clicking expecting ``ready`` would get
    ECONNREFUSED. With the wiring, ``missing`` colours red and the
    operator sees the actual state.
    """
    _stage_active(
        tmp_path,
        {
            "mcp": {
                "http_url": "http://127.0.0.1:50001/sse",
                "healthz_url": "http://127.0.0.1:50001/healthz",
                "pid": 50001,
            }
        },
    )
    monkeypatch.setattr(
        "ctxr.fsm.cli.urls_cmd._live_status_for_subsystem",
        lambda _block: "missing",
    )

    result = runner.invoke(app, ["urls", "--project-root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "missing" in result.output
