"""End-to-end smoke tests for the runnable scripts under ``examples/``.

Each test invokes one example as a subprocess (using the same Python
interpreter that is running pytest), waits for it to finish, and
asserts that:

* the process exited 0,
* the stdout contains the marker lines that prove the FSM reached
  its expected terminal verdict / final state.

The example scripts are pure-Python and self-contained — they open a
temporary SQLite database, drive the FSM with simulated worker
outputs, and print the final state-tree + event log to stdout. See
``examples/README.md`` for the per-example narrative.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# Repo root = fsm/. ``tests/examples/test_examples_run_clean.py`` is
# two levels deep, so two ``parent`` hops land us at the fsm/ root
# where ``examples/`` lives.
REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES_DIR = REPO_ROOT / "examples"


def _run_example(script_name: str) -> subprocess.CompletedProcess[str]:
    """Run ``examples/<script_name>`` as a subprocess and return the result.

    We invoke the example via ``sys.executable`` (rather than the
    ``uv``-managed ``python``) so the test inherits whatever
    interpreter pytest is running under — that is the interpreter
    pytest already verified the project's dependencies are installed
    into.
    """
    script_path = EXAMPLES_DIR / script_name
    assert script_path.is_file(), f"missing example script: {script_path}"

    proc = subprocess.run(
        [sys.executable, str(script_path)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    return proc


def _assert_lines_in_stdout(
    proc: subprocess.CompletedProcess[str],
    expected_substrings: list[str],
) -> None:
    """Assert each ``expected_substrings`` entry occurs in ``proc.stdout``.

    On failure dumps the full stdout + stderr into the AssertionError
    message so the failure is debuggable from CI logs without re-
    running the test locally.
    """
    missing = [s for s in expected_substrings if s not in proc.stdout]
    if missing:
        raise AssertionError(
            "expected substrings missing from example stdout:\n"
            f"  missing  : {missing!r}\n"
            f"  exit code: {proc.returncode}\n"
            f"--- stdout ---\n{proc.stdout}\n"
            f"--- stderr ---\n{proc.stderr}\n"
        )


def test_plan_implement_qa_fix_runs_clean() -> None:
    """``plan_implement_qa_fix.py`` reaches ``done`` with a ``GO`` verdict.

    The first QA pass returns ``NO-GO`` (routes to ``fix``), the
    second QA pass returns ``GO`` and routes to ``done`` — so the
    final stdout must report ``status: completed``, ``verdict: GO``,
    and ``current: done``.
    """
    proc = _run_example("plan_implement_qa_fix.py")
    assert proc.returncode == 0, (
        f"example exited {proc.returncode}\n"
        f"--- stdout ---\n{proc.stdout}\n"
        f"--- stderr ---\n{proc.stderr}\n"
    )
    _assert_lines_in_stdout(
        proc,
        [
            "status     : completed",
            "verdict    : GO",
            "current    : done",
            "final_state : done",
            # state_tree fingerprint: the second qa entry proves the
            # NO-GO → fix → qa → GO branch fired before the run
            # terminated.
            "- fix (seq=4, status=exited)",
            "- qa (seq=5, status=exited)",
        ],
    )


def test_code_review_pipeline_runs_clean() -> None:
    """``code_review_pipeline.py`` reaches ``done`` with a ``CONDITIONAL`` verdict.

    The fan-out loop produces one BLOCKER finding that carries a
    ``suggested_fix``, so the GO/CONDITIONAL/NO-GO rule resolves to
    ``CONDITIONAL``. The cross-state aggregator emits an
    ``aggregate_built`` event between ``dispatch_lenses`` and
    ``collect_findings`` — its presence in the event log is part of
    the contract we assert on.
    """
    proc = _run_example("code_review_pipeline.py")
    assert proc.returncode == 0, (
        f"example exited {proc.returncode}\n"
        f"--- stdout ---\n{proc.stdout}\n"
        f"--- stderr ---\n{proc.stderr}\n"
    )
    _assert_lines_in_stdout(
        proc,
        [
            "== code_review_pipeline.py ==",
            "status     : completed",
            "verdict    : CONDITIONAL",
            "final_state: done",
            "aggregate_built",
            "run_completed",
        ],
    )


def test_research_with_retries_runs_clean() -> None:
    """``research_with_retries.py`` reaches ``publish`` and the recovery demo discards a pending txn.

    The reviewer rejects the first draft (``verdict='retry'``), the
    research loop re-enters, the second review accepts
    (``verdict='accept'``) and the run lands in ``publish``. The
    recovery demo then injects a pending journal txn and clears it
    via ``journal.discard``.

    The example doesn't print the literal strings ``verdict: accept``
    or ``recovery: discarded`` — instead it surfaces the same
    invariants through the SUMMARY block: ``Main run final state:
    publish`` (proof the accept branch fired) and
    ``recover_journal action applied: discard`` (proof the discard
    path ran), with ``Recovery demonstrated: True`` as the final
    sentinel. We assert all three.
    """
    proc = _run_example("research_with_retries.py")
    assert proc.returncode == 0, (
        f"example exited {proc.returncode}\n"
        f"--- stdout ---\n{proc.stdout}\n"
        f"--- stderr ---\n{proc.stderr}\n"
    )
    _assert_lines_in_stdout(
        proc,
        [
            # MAIN RUN: accept branch fired -> publish is terminal.
            "MAIN RUN — retry-then-converge",
            "Main run final state:     publish",
            "Final published location: docs/research.md",
            "Journal clean after main run: True",
            # RECOVERY DEMO: discard path executed cleanly.
            "RECOVERY DEMO — simulated crash + journal cleanup",
            "recover_journal action applied: discard",
            "Journal clean after recovery: True",
            "Recovery demonstrated:    True",
        ],
    )
