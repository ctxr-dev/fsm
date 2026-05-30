"""Warm-path coverage for ``ctxr-fsm ensure`` (W14b).

Simulates a warm project by:

1. Running ``ctxr-fsm init`` to land the DB.
2. Synthesising live ``pids/<name>.pid`` records + an
   ``active-mcp.json`` document that point at this very test process
   (so the pid liveness check passes) and a fake but answerable
   healthz endpoint (``httpx`` is mocked via monkeypatch).

We assert that ``ensure`` in this state returns ``status: ready`` with
``actions.supervisor = reused`` in well under the 2000ms budget
(brief threshold) and never spawns a subprocess.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest

from ctxr.fsm.cli import ensure_cmd
from ctxr.fsm.cli.ensure_cmd import run_ensure
from ctxr.fsm.cli.init_cmd import run_init
from ctxr.fsm.cli.lifecycle.primitives import remember_active_mcp_file


def _stage_pids(project_root: Path, names: list[str]) -> None:
    """Write a singleton pid file per name pointing at this process.

    A live pid + matching probe URL is what ``_probe_subsystem_alive``
    treats as "subsystem is up"; pointing at ``os.getpid()`` makes
    the liveness probe trivially succeed.
    """
    pids_dir = project_root / ".ctxr-fsm" / "pids"
    pids_dir.mkdir(parents=True, exist_ok=True)
    self_pid = os.getpid()
    for name in names:
        (pids_dir / f"{name}.pid").write_text(
            json.dumps(
                {
                    "name": name,
                    "pid": self_pid,
                    "probe_url": "http://127.0.0.1:65000",
                    "acquired_at": "2026-05-29T00:00:00.000+00:00",
                },
                sort_keys=True,
                indent=2,
            ),
            encoding="utf-8",
        )


def _stage_active_mcp_doc(project_root: Path, *, subsystems: list[str]) -> None:
    """Write a fake but well-formed discovery doc.

    The URLs need to point at an endpoint our mocked httpx will
    answer — but we mock httpx wholesale so the value just needs to
    be a string.
    """
    sub_payload: dict[str, Any] = {}
    for name in subsystems:
        block = {
            "http_url": f"http://127.0.0.1:65000{'/sse' if name == 'mcp' else ''}",
            "healthz_url": (
                "http://127.0.0.1:65000/healthz" if name != "ui" else None
            ),
            "pid": os.getpid(),
        }
        if name == "api":
            block["docs_url"] = "http://127.0.0.1:65000/docs"
        sub_payload[name] = block
    remember_active_mcp_file(
        {
            "started_at": "2026-05-29T00:00:00.000+00:00",
            "supervisor_pid": os.getpid(),
            "version": "0.0.0-test",
            "subsystems": sub_payload,
        },
        project_root=project_root,
    )


def _mock_httpx_get_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Force ``httpx.get`` to always return a 200 response.

    The warm-path test mustn't make real network calls — both the
    pid-probe healthz and the wait-loop healthz get patched.
    """

    class _Resp:
        status_code = 200
        text = "ok"

    def _fake_get(url: str, *args: Any, **kwargs: Any) -> _Resp:
        return _Resp()

    monkeypatch.setattr(httpx, "get", _fake_get)


def test_warm_path_is_under_2000ms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-baked warm project exits ensure in < 2000ms with reused.

    The brief says <500ms is the goal but CI flakiness justifies the
    looser 2000ms ceiling for the assertion.
    """
    # 1. Real init so the DB + alembic head land.
    run_init(
        db_path=tmp_path / ".ctxr-fsm" / "fsm.db",
        no_memory=True,
        cwd=tmp_path,
    )
    # 2. Stage live pids + discovery doc.
    _stage_pids(tmp_path, names=["mcp", "api", "ui"])
    _stage_active_mcp_doc(tmp_path, subsystems=["mcp", "api", "ui"])
    # 3. Mock httpx so the healthz probes succeed without a real
    #    listener on the port.
    _mock_httpx_get_ok(monkeypatch)
    # 4. Avoid touching any client-config files: skip both memory and
    #    mcp-config.
    summary = run_ensure(
        project_root=tmp_path,
        client="none",
        mode="full",
        no_memory=True,
        no_mcp_config=True,
        check=False,
        timeout=5.0,
    )
    assert summary["status"] == "ready", summary
    assert summary["actions"]["supervisor"] == "reused"
    assert summary["actions"]["init"] == "current"
    assert summary["actions"]["memory"] == "skipped"
    assert summary["actions"]["mcp_config"] == "skipped"
    # Hard duration assertion: < 2s warm path on CI.
    assert summary["duration_ms"] < 2000, summary

    # Belt-and-braces: no subprocess was spawned (no ``spawned_supervisor_pid``).
    assert "spawned_supervisor_pid" not in summary


def test_warm_path_does_not_spawn_when_already_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replace ``_spawn_supervisor_detached`` with a sentinel and confirm it is never called."""
    run_init(
        db_path=tmp_path / ".ctxr-fsm" / "fsm.db",
        no_memory=True,
        cwd=tmp_path,
    )
    _stage_pids(tmp_path, names=["mcp", "api", "ui"])
    _stage_active_mcp_doc(tmp_path, subsystems=["mcp", "api", "ui"])
    _mock_httpx_get_ok(monkeypatch)

    spawn_calls: list[bool] = []

    def _fail_spawn(**_kwargs: Any) -> int:
        spawn_calls.append(True)
        raise RuntimeError("ensure must not spawn when warm")

    monkeypatch.setattr(
        ensure_cmd, "_spawn_supervisor_detached", _fail_spawn
    )

    summary = run_ensure(
        project_root=tmp_path,
        client="none",
        mode="full",
        no_memory=True,
        no_mcp_config=True,
        check=False,
        timeout=5.0,
    )
    assert spawn_calls == []
    assert summary["actions"]["supervisor"] == "reused"


def test_mcp_only_warm_only_probes_mcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In ``mcp-only`` mode the absence of api+ui pids is OK if mcp is up."""
    run_init(
        db_path=tmp_path / ".ctxr-fsm" / "fsm.db",
        no_memory=True,
        cwd=tmp_path,
    )
    _stage_pids(tmp_path, names=["mcp"])
    _stage_active_mcp_doc(tmp_path, subsystems=["mcp"])
    _mock_httpx_get_ok(monkeypatch)

    summary = run_ensure(
        project_root=tmp_path,
        client="none",
        mode="mcp_only",
        no_memory=True,
        no_mcp_config=True,
        check=False,
        timeout=5.0,
    )
    assert summary["status"] == "ready", summary
    assert summary["actions"]["supervisor"] == "reused"
    # Only mcp listed in the subsystems block.
    assert set(summary["subsystems"].keys()) == {"mcp"}
