"""``ctxr-fsm ensure --check`` is read-only (W14b).

The check mode is the cheapest probe a skill can run: it must never
spawn the supervisor, never mutate any file, and return per-step
status in a single JSON document.
"""

from __future__ import annotations

from pathlib import Path

from ctxr.fsm.cli.ensure_cmd import run_ensure


def test_check_on_fresh_dir_reports_missing(tmp_path: Path) -> None:
    """A fresh tmpdir with no .ctxr-fsm yet returns ``status: missing:...``.

    Asserts:

    * No ``.ctxr-fsm`` dir is created.
    * No supervisor process is spawned (pids dir is empty).
    * The summary lists ``init`` and ``supervisor`` as missing.
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
    # W14i: ``status`` is one of the ``missing_*`` enum members rather
    # than a colon-prefixed list. When multiple steps are missing the
    # most-upstream one (init) wins so the wrapping script knows which
    # gate fired first; per-step granularity remains on ``actions``.
    assert summary["status"] == "missing_init"
    assert summary["actions"]["init"] == "missing"
    assert summary["actions"]["supervisor"] == "missing"
    # mcp-config skipped because client=none.
    assert summary["actions"]["mcp_config"] == "skipped"
    # No filesystem mutation.
    assert not (tmp_path / ".ctxr-fsm").exists()


def test_check_is_fast(tmp_path: Path) -> None:
    """Even on cold check the run completes in well under the user-facing budget.

    1000ms is a generous CI threshold; the smoke run earlier showed
    1ms locally. A check that exceeds this is almost certainly
    accidentally spawning something.
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
    assert summary["duration_ms"] < 1000, summary


def test_check_does_not_touch_pid_files(tmp_path: Path) -> None:
    """Verify no pid files are created by --check."""
    (tmp_path / ".ctxr-fsm" / "pids").mkdir(parents=True)
    run_ensure(
        project_root=tmp_path,
        client="none",
        mode="full",
        no_memory=True,
        no_mcp_config=True,
        check=True,
        timeout=2.0,
    )
    pids_dir = tmp_path / ".ctxr-fsm" / "pids"
    assert list(pids_dir.iterdir()) == [], list(pids_dir.iterdir())
