"""Integration coverage for the W14j Rich subsystem table in ``ensure``.

The ensure command's pretty surface gained (W14j) a shared Rich table
that lists every subsystem URL + Swagger + status + PID. These tests
pin the matrix of TTY-detection + ``--json`` + ``--no-table`` flag
combinations so a regression in any axis trips loudly.

Pattern (mirrors ``test_ensure_warm_fast_path.py``):

1. Real ``run_init`` to land the DB + alembic head.
2. Stage live ``pids/<name>.pid`` records pointing at this process so
   the liveness probe trivially succeeds.
3. Stage an ``active-mcp.json`` discovery document with all three
   subsystem blocks.
4. Mock ``httpx.get`` to always return 200 so the warm-path probes
   succeed without a real listener.
5. Force ``sys.stdout.isatty`` to return ``True`` (or ``False``) so
   the TTY-detect branch picks the table-on or table-off path.
6. Drive ``ctxr-fsm ensure`` through the typer ``CliRunner`` and
   assert on the captured stdout.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from ctxr.fsm.cli import app
from ctxr.fsm.cli.init_cmd import run_init
from ctxr.fsm.cli.lifecycle.primitives import remember_active_mcp_file

runner = CliRunner()


# ---------------------------------------------------------------------------
# Fixture staging (duplicated from test_ensure_warm_fast_path so this file
# stays self-contained; the helpers are ~10 lines each and SRP-aligned).
# ---------------------------------------------------------------------------


def _stage_pids(project_root: Path, names: list[str]) -> None:
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
                    "acquired_at": "2026-05-30T00:00:00.000+00:00",
                },
                sort_keys=True,
                indent=2,
            ),
            encoding="utf-8",
        )


def _stage_active_mcp_doc(project_root: Path, *, subsystems: list[str]) -> None:
    sub_payload: dict[str, Any] = {}
    for name in subsystems:
        block: dict[str, Any] = {
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
            "started_at": "2026-05-30T00:00:00.000+00:00",
            "supervisor_pid": os.getpid(),
            "version": "0.0.0-test",
            "subsystems": sub_payload,
        },
        project_root=project_root,
    )


def _mock_httpx_get_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Resp:
        status_code = 200
        text = "ok"

    def _fake_get(url: str, *args: Any, **kwargs: Any) -> _Resp:
        return _Resp()

    monkeypatch.setattr(httpx, "get", _fake_get)


def _force_tty(monkeypatch: pytest.MonkeyPatch, *, is_tty: bool) -> None:
    """Make ``ensure_cmd``'s view of ``sys.stdout.isatty()`` return ``is_tty``.

    ``typer.testing.CliRunner`` replaces ``sys.stdout`` for the duration
    of an invocation, so a naive ``monkeypatch.setattr(sys, "stdout",
    ...)`` is clobbered the moment ``runner.invoke`` starts. We patch
    the helpers ensure_cmd uses to make the TTY decision directly:

    * ``ensure_cmd._json_default`` — returns ``False`` when a TTY is
      claimed, so the default JSON-on-pipe behaviour matches an
      interactive terminal.
    * ``ensure_cmd.sys.stdout.isatty`` — the W14j table-render branch
      probes this independently of ``--json``. We patch ``sys`` in
      the module's namespace with a shim whose ``stdout.isatty()``
      returns our chosen value, so the module's import-time reference
      to ``sys`` resolves through us regardless of what CliRunner has
      done to the real ``sys.stdout``.
    """
    import sys as _real_sys

    from ctxr.fsm.cli import ensure_cmd

    class _StdoutShim:
        def isatty(self) -> bool:
            return is_tty

        def __getattr__(self, item: str) -> Any:
            return getattr(_real_sys.stdout, item)

    class _SysShim:
        stdout = _StdoutShim()

        def __getattr__(self, item: str) -> Any:
            return getattr(_real_sys, item)

    monkeypatch.setattr(ensure_cmd, "sys", _SysShim())
    monkeypatch.setattr(ensure_cmd, "_json_default", lambda: not is_tty)


def _stage_warm_project(
    tmp_path: Path, *, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Land DB + pids + discovery doc + httpx mock; return the project root."""
    run_init(
        db_path=tmp_path / ".ctxr-fsm" / "fsm.db",
        no_memory=True,
        cwd=tmp_path,
    )
    _stage_pids(tmp_path, names=["mcp", "api", "ui"])
    _stage_active_mcp_doc(tmp_path, subsystems=["mcp", "api", "ui"])
    _mock_httpx_get_ok(monkeypatch)
    return tmp_path


# ---------------------------------------------------------------------------
# Tests — matrix coverage
# ---------------------------------------------------------------------------


def test_ensure_tty_non_json_prints_subsystem_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TTY + ``--no-json`` renders the Rich subsystem table after the actions summary.

    The actions-summary dict still prints (so operators see init /
    memory / mcp_config / supervisor status); the table appears
    below. Headers + at least one URL substring must survive in the
    captured stdout.
    """
    _stage_warm_project(tmp_path, monkeypatch=monkeypatch)
    _force_tty(monkeypatch, is_tty=True)

    result = runner.invoke(
        app,
        [
            "ensure",
            "--project-root",
            str(tmp_path),
            "--client",
            "none",
            "--mode",
            "full",
            "--no-memory",
            "--no-mcp-config",
            "--no-json",
        ],
    )
    assert result.exit_code == 0, result.stdout

    # The Rich CliRunner sandbox is fixed at 80 columns so the
    # table's first column header ("Subsystem") elides to "Sub…" —
    # don't pin the table header text in this runner. Instead assert
    # on tokens that genuinely survive: row labels, the URL block
    # header (one of the W16 deliverables), and the staged URL.
    assert "mcp" in result.stdout
    assert "Open in your browser" in result.stdout
    assert "127.0.0.1:65000" in result.stdout


def test_ensure_json_mode_does_not_print_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--json`` (regardless of TTY) skips the table entirely.

    The JSON wire contract is the machine consumer's surface — adding
    table characters to that stream would break every script that
    pipes ``ensure --json | jq``.
    """
    _stage_warm_project(tmp_path, monkeypatch=monkeypatch)
    _force_tty(monkeypatch, is_tty=True)

    result = runner.invoke(
        app,
        [
            "ensure",
            "--project-root",
            str(tmp_path),
            "--client",
            "none",
            "--mode",
            "full",
            "--no-memory",
            "--no-mcp-config",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.stdout

    # No Rich table characters in the output. The Subsystem column
    # header is the cleanest single sentinel.
    assert "Subsystem" not in result.stdout
    assert "Open in your browser" not in result.stdout
    # The captured stdout is parseable JSON.
    payload = json.loads(result.stdout)
    assert payload["status"] == "ready"


def test_ensure_no_table_flag_suppresses_table_in_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--no-table`` in TTY non-JSON mode keeps the actions summary but drops the table."""
    _stage_warm_project(tmp_path, monkeypatch=monkeypatch)
    _force_tty(monkeypatch, is_tty=True)

    result = runner.invoke(
        app,
        [
            "ensure",
            "--project-root",
            str(tmp_path),
            "--client",
            "none",
            "--mode",
            "full",
            "--no-memory",
            "--no-mcp-config",
            "--no-json",
            "--no-table",
        ],
    )
    assert result.exit_code == 0, result.stdout

    # Actions-summary text path still prints (the dict goes through
    # rich.print so the action keys appear as string substrings).
    assert "actions" in result.stdout or "supervisor" in result.stdout
    # The table header is absent.
    assert "Subsystem" not in result.stdout
    assert "Open in your browser" not in result.stdout


def test_ensure_no_tty_defaults_to_json_without_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-TTY stdout: ``_json_default`` returns True → JSON only, no table.

    Pipe-from-script case. The default ``--json`` flip means the
    table-render branch never fires either way (JSON mode short-
    circuits before the TTY check), but we keep this test to lock the
    contract from the *script consumer's* angle.
    """
    _stage_warm_project(tmp_path, monkeypatch=monkeypatch)
    _force_tty(monkeypatch, is_tty=False)

    result = runner.invoke(
        app,
        [
            "ensure",
            "--project-root",
            str(tmp_path),
            "--client",
            "none",
            "--mode",
            "full",
            "--no-memory",
            "--no-mcp-config",
        ],
    )
    assert result.exit_code == 0, result.stdout
    # JSON parses, table is absent.
    payload = json.loads(result.stdout)
    assert payload["status"] == "ready"
    assert "Subsystem" not in result.stdout
