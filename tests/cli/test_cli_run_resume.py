"""CLI tests for ``ctxr-fsm run resume``.

Covers the three operator-visible behaviours of the W3 ``run resume``
command:

* ``--journal discard`` removes a pending journal_txns row, so a
  subsequent :meth:`JournalRepo.inspect` returns ``None``.
* ``--journal replay`` does *not* discard a ``ready_to_finalise`` row —
  it records the operator's intent without touching the journal, and
  the row remains visible to ``inspect``. (Engine-driven replay-into-
  state lands in W12.)
* ``--from-state <state>`` prints the W3 stub message making the
  deferral explicit, so operators do not silently assume the engine
  has picked the run back up.

Each test allocates its own temp directory + SQLite DB so the suite
is fully isolated; the CLI is exercised through Typer's
:class:`CliRunner` so we cover the argument-parsing surface alongside
the command body.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ctxr.fsm.cli import app
from ctxr.fsm.core.models import FsmSpec, State, Transition, Worker
from ctxr.fsm.sqlite import Project

runner = CliRunner()


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _minimal_spec(spec_id: str = "demo") -> FsmSpec:
    """Two-state FsmSpec with one ``always`` transition.

    The substrate needs an entry state plus at least one transition to
    register the spec and start a run; everything else about the FSM is
    irrelevant for the resume bookkeeping under test.
    """
    states = [
        State(
            id="start",
            purpose="starting state",
            worker=Worker(role="start", prompt_template="go"),
            transitions=[Transition(to="finish", when="always")],
        ),
        State(
            id="finish",
            purpose="terminal state",
            worker=Worker(role="finish", prompt_template="done"),
            transitions=[],
        ),
    ]
    return FsmSpec(id=spec_id, entry="start", states=states)


@pytest.fixture()
def project_db_path() -> Iterator[Path]:
    """Yield a path to a fresh project SQLite file.

    The path is *not* yet populated — callers are expected to drive the
    CLI (or :class:`Project`) against it, which performs the migration
    on first open.
    """
    with tempfile.TemporaryDirectory() as td:
        yield Path(td) / "fsm.db"


def _seed_run_with_pending_journal(db_path: Path) -> str:
    """Create a project, register a spec, start a run, open a journal txn.

    Returns the run id. The journal row is left in ``pending`` status so
    tests can exercise ``--journal discard`` against it.
    """
    project = Project.open(db_path, migrate=True, echo=False)
    try:
        registered = project.register_spec(_minimal_spec())
        run = project.start_run(spec_id=registered.spec.id)
        with project.session_factory() as session, session.begin():
            project.journal.open(session, run_id=run.id)
        return run.id
    finally:
        project.close()


def _seed_run_with_ready_journal(db_path: Path) -> str:
    """Create a run with a ``ready_to_finalise`` journal row.

    Returns the run id. Used by the ``--journal replay`` test, which
    needs a row in the "staged but not yet committed" state to assert
    that replay-intent does not remove it.
    """
    project = Project.open(db_path, migrate=True, echo=False)
    try:
        registered = project.register_spec(_minimal_spec())
        run = project.start_run(spec_id=registered.spec.id)
        with project.session_factory() as session, session.begin():
            txn = project.journal.open(session, run_id=run.id)
            project.journal.mark_ready(
                session,
                txn_id=txn.id,
                staged_writes=[{"path": "manifest.json", "hash": "abc123"}],
            )
        return run.id
    finally:
        project.close()


def _journal_snapshot(db_path: Path, run_id: str):
    """Return the newest unfinalised journal row for ``run_id`` (or ``None``).

    Re-opens the project read-only-ish (we only call ``inspect`` which
    does not begin a transaction) so the assertion observes whatever
    the CLI committed.
    """
    project = Project.open(db_path, migrate=True, echo=False)
    try:
        with project.session_factory() as session:
            return project.journal.inspect(session, run_id=run_id)
    finally:
        project.close()


# ---------------------------------------------------------------------------
# --journal discard
# ---------------------------------------------------------------------------


def test_run_resume_journal_discard_removes_pending_row(
    project_db_path: Path,
) -> None:
    """``--journal discard`` deletes the pending journal_txns row.

    We seed a pending row, invoke ``ctxr-fsm run resume <id> --journal
    discard``, then assert that :meth:`JournalRepo.inspect` returns
    ``None`` and the CLI payload reports ``journal_action='discarded'``.
    """
    run_id = _seed_run_with_pending_journal(project_db_path)

    # Sanity check: a pending row really does exist before we resume.
    pre = _journal_snapshot(project_db_path, run_id)
    assert pre is not None
    assert pre.status == "pending"

    result = runner.invoke(
        app,
        [
            "run",
            "resume",
            run_id,
            "--journal",
            "discard",
            "--db",
            str(project_db_path),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["run_id"] == run_id
    assert payload["journal_action"] == "discarded"
    assert payload["journal_txn_id"] == pre.id

    # The row must be gone after discard.
    post = _journal_snapshot(project_db_path, run_id)
    assert post is None


# ---------------------------------------------------------------------------
# --journal replay
# ---------------------------------------------------------------------------


def test_run_resume_journal_replay_finalises_ready_row(
    project_db_path: Path,
) -> None:
    """``--journal replay`` against a ``ready_to_finalise`` row records intent.

    In W3 the command does not actually re-apply the staged writes
    (that lands in W12); it records ``journal_action='replay_requested'``
    and leaves the journal row in place so the engine can pick it up
    later. We assert both halves of that contract: the CLI reports the
    correct action, and the row is still visible to ``inspect`` with
    its original ``ready_to_finalise`` status.
    """
    run_id = _seed_run_with_ready_journal(project_db_path)

    pre = _journal_snapshot(project_db_path, run_id)
    assert pre is not None
    assert pre.status == "ready_to_finalise"

    result = runner.invoke(
        app,
        [
            "run",
            "resume",
            run_id,
            "--journal",
            "replay",
            "--db",
            str(project_db_path),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["run_id"] == run_id
    assert payload["journal_action"] == "replay_requested"
    assert payload["journal_txn_id"] == pre.id

    # The ready_to_finalise row must still be present — replay is
    # bookkeeping in W3, not destruction.
    post = _journal_snapshot(project_db_path, run_id)
    assert post is not None
    assert post.id == pre.id
    assert post.status == "ready_to_finalise"


# ---------------------------------------------------------------------------
# --from-state <state>
# ---------------------------------------------------------------------------


def test_run_resume_from_state_prints_w3_stub_message(
    project_db_path: Path,
) -> None:
    """``--from-state <state>`` emits the W3 stub deferral message.

    Engine-driven resume from an arbitrary state lands in W12; today
    the CLI records the operator's intent in the emitted event and
    surfaces a one-line notice so scripts and humans see the deferral
    explicitly. We exercise both output modes:

    * The JSON payload exposes ``engine_resume`` carrying the deferral
      message — that is the stable contract for scripted consumers.
    * The non-JSON (pretty) output also mentions the deferral so a
      human eyeballing the terminal sees it without having to add
      ``--json``.
    """
    # Seed a run; no journal row needed for this case.
    project = Project.open(project_db_path, migrate=True, echo=False)
    try:
        registered = project.register_spec(_minimal_spec())
        run = project.start_run(spec_id=registered.spec.id)
        run_id = run.id
    finally:
        project.close()

    # ── JSON mode ───────────────────────────────────────────────────────
    json_result = runner.invoke(
        app,
        [
            "run",
            "resume",
            run_id,
            "--from-state",
            "start",
            "--db",
            str(project_db_path),
            "--json",
        ],
    )
    assert json_result.exit_code == 0, json_result.output
    payload = json.loads(json_result.stdout)
    assert payload["run_id"] == run_id
    assert payload["from_state"] == "start"
    # No --journal flag was passed, so journal_action stays unset.
    assert payload["journal_action"] is None
    # The stub message is the load-bearing assertion: it tells the
    # operator engine-driven resume is not yet wired up.
    assert "W12" in payload["engine_resume"]
    assert "later workstream" in payload["engine_resume"]

    # ── Pretty mode ─────────────────────────────────────────────────────
    pretty_result = runner.invoke(
        app,
        [
            "run",
            "resume",
            run_id,
            "--from-state",
            "start",
            "--db",
            str(project_db_path),
        ],
    )
    assert pretty_result.exit_code == 0, pretty_result.output
    # rich.print may colourise but the bare token "W12" survives any
    # styling and is the most unambiguous deferral marker.
    assert "W12" in pretty_result.output
