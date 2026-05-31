"""CLI tests for ``ctxr-fsm run abort``.

Coverage:

* Aborting an in-progress run flips the manifest's ``status`` to
  ``aborted`` and stamps ``ended_at``.
* The same invocation emits a ``run_aborted`` event whose payload
  carries the operator-supplied reason and the previous status.
* The CLI refuses to abort a run that is already in a terminal state
  (``completed`` / ``aborted``) — the manifest stays untouched and no
  spurious ``run_aborted`` event is appended.
* Unknown run ids exit non-zero before any side effect lands.
* ``--json`` produces a machine-readable payload that agrees with the
  rows the command just wrote.

The tests drive the CLI through ``typer.testing.CliRunner`` against the
real ``ctxr.fsm.cli:app`` and a fresh per-test SQLite database under
``tempfile.TemporaryDirectory``. They assert against the substrate
through the public ``Project`` facade so the contract under test is the
"what landed in the DB" surface, not the implementation detail of which
repo method got called.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ctxr.fsm.cli import app
from ctxr.fsm.core.models import EventKind, FsmSpec, State, Transition
from ctxr.fsm.sqlite import Project

runner = CliRunner()


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_spec() -> FsmSpec:
    """Return a tiny linear two-state spec used to bootstrap test runs.

    Shape: ``a`` (entry) → ``b`` (terminal). We never actually drive a
    transition in these tests — they only need a run row to exist so
    they can flip its status — but the spec must be at least one
    well-formed FSM for ``register_spec`` to accept it.
    """
    return FsmSpec(
        id="abort_test_spec",
        version=1,
        entry="a",
        states=[
            State(id="a", transitions=[Transition(to="b", when="always")]),
            State(id="b"),
        ],
    )


@pytest.fixture
def db_path() -> Iterator[Path]:
    """Yield a fresh project DB path under a per-test temp dir."""
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp) / "fsm.db"


def _seed_run(db: Path) -> str:
    """Bootstrap a project DB at ``db`` and return a new run id.

    Uses ``Project.open(migrate=True)`` so the schema gets created on
    first touch, the same way the CLI itself opens it.
    """
    with Project.open(db, migrate=True, echo=False) as project:
        registered = project.register_spec(_make_spec())
        run = project.start_run(registered.spec.id, args={})
        return run.id


def _read_run_status(db: Path, run_id: str) -> tuple[str, str | None]:
    """Return ``(status, ended_at)`` for ``run_id`` from the on-disk DB."""
    with Project.open(db, migrate=False, echo=False) as project:
        run = project.get_run(run_id)
        assert run is not None, f"run {run_id!r} vanished between writes"
        return run.status, run.ended_at


def _read_event_kinds(db: Path, run_id: str) -> list[str]:
    """Return every event kind recorded against ``run_id``, in seq order."""
    with (
        Project.open(db, migrate=False, echo=False) as project,
        project.session_factory() as session,
    ):
        events = list(project.runs.events(session, run_id))
    return [event.kind for event in events]


def _read_last_event_payload(db: Path, run_id: str) -> dict:
    """Return the payload of the most-recent event recorded on ``run_id``."""
    with (
        Project.open(db, migrate=False, echo=False) as project,
        project.session_factory() as session,
    ):
        events = list(project.runs.events(session, run_id))
    assert events, f"no events recorded on run {run_id!r}"
    return events[-1].payload


def _force_run_status(db: Path, run_id: str, status: str) -> None:
    """Manually flip the run's status to ``status`` for terminal-state tests.

    The CLI refuses to abort a run that is already terminal; to exercise
    that branch we need to put the run *into* a terminal state without
    re-implementing the abort command. We go through ``RunsRepo``
    directly so the row is updated atomically with the same shape the
    engine itself would write.
    """
    with (
        Project.open(db, migrate=False, echo=False) as project,
        project.session_factory() as session,
        session.begin(),
    ):
        updated = project.runs.update_status(
            session,
            run_id=run_id,
            status=status,
            ended_at=None,
        )
        assert updated is not None, f"run {run_id!r} not found"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_run_abort_marks_run_aborted_and_emits_event(db_path: Path) -> None:
    """Happy path: ``run abort`` flips status, stamps ended_at, emits event."""
    run_id = _seed_run(db_path)

    # Sanity check: the run starts in_progress with no ended_at.
    status, ended_at = _read_run_status(db_path, run_id)
    assert status == "in_progress"
    assert ended_at is None

    result = runner.invoke(
        app,
        ["run", "abort", run_id, "--db", str(db_path), "--reason", "user cancelled"],
    )
    assert result.exit_code == 0, result.output

    # Manifest reflects the abort.
    status, ended_at = _read_run_status(db_path, run_id)
    assert status == "aborted"
    assert ended_at is not None and ended_at != ""

    # A ``run_aborted`` event landed on the bus exactly once.
    kinds = _read_event_kinds(db_path, run_id)
    assert kinds.count(EventKind.run_aborted.value) == 1
    # And it is the *last* event on the timeline.
    assert kinds[-1] == EventKind.run_aborted.value
    # The first event is still the ``run_started`` recorded by start_run.
    assert kinds[0] == EventKind.run_started.value

    payload = _read_last_event_payload(db_path, run_id)
    assert payload["run_id"] == run_id
    assert payload["reason"] == "user cancelled"
    assert payload["previous_status"] == "in_progress"
    assert payload["ended_at"] == ended_at


def test_run_abort_without_reason_records_null_reason(db_path: Path) -> None:
    """Omitting ``--reason`` still aborts; payload records ``reason=None``."""
    run_id = _seed_run(db_path)

    result = runner.invoke(app, ["run", "abort", run_id, "--db", str(db_path)])
    assert result.exit_code == 0, result.output

    status, ended_at = _read_run_status(db_path, run_id)
    assert status == "aborted"
    assert ended_at is not None

    payload = _read_last_event_payload(db_path, run_id)
    assert payload["reason"] is None
    assert payload["previous_status"] == "in_progress"


def test_run_abort_json_mode_emits_machine_payload(db_path: Path) -> None:
    """``--json`` prints a parsable payload that mirrors the DB write."""
    run_id = _seed_run(db_path)

    result = runner.invoke(
        app,
        [
            "run",
            "abort",
            run_id,
            "--db",
            str(db_path),
            "--reason",
            "shutdown",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output

    payload = json.loads(result.stdout)
    assert payload["run_id"] == run_id
    assert payload["previous_status"] == "in_progress"
    assert payload["new_status"] == "aborted"
    assert payload["reason"] == "shutdown"
    assert payload["ended_at"]

    # The JSON payload's ended_at must match what the substrate persisted.
    status, ended_at = _read_run_status(db_path, run_id)
    assert status == "aborted"
    assert payload["ended_at"] == ended_at


def test_run_abort_refuses_terminal_run(db_path: Path) -> None:
    """Re-aborting a ``completed`` or ``aborted`` run exits non-zero."""
    run_id = _seed_run(db_path)
    _force_run_status(db_path, run_id, "completed")

    kinds_before = _read_event_kinds(db_path, run_id)

    result = runner.invoke(
        app, ["run", "abort", run_id, "--db", str(db_path)]
    )
    assert result.exit_code != 0
    # Error message names the offending status so the operator knows why.
    assert "completed" in (result.stderr or "") or "completed" in (result.output or "")

    # Manifest must be untouched.
    status, _ended_at = _read_run_status(db_path, run_id)
    assert status == "completed"

    # No spurious ``run_aborted`` event was appended.
    kinds_after = _read_event_kinds(db_path, run_id)
    assert kinds_after == kinds_before
    assert EventKind.run_aborted.value not in kinds_after


def test_run_abort_refuses_already_aborted_run(db_path: Path) -> None:
    """Aborting twice in a row is a no-op on the second call."""
    run_id = _seed_run(db_path)

    first = runner.invoke(app, ["run", "abort", run_id, "--db", str(db_path)])
    assert first.exit_code == 0, first.output

    kinds_after_first = _read_event_kinds(db_path, run_id)
    assert kinds_after_first.count(EventKind.run_aborted.value) == 1

    second = runner.invoke(app, ["run", "abort", run_id, "--db", str(db_path)])
    assert second.exit_code != 0
    assert "aborted" in (second.stderr or "") or "aborted" in (second.output or "")

    # Still only one run_aborted event on the timeline.
    kinds_after_second = _read_event_kinds(db_path, run_id)
    assert kinds_after_second == kinds_after_first


def test_run_abort_unknown_run_id_exits_nonzero(db_path: Path) -> None:
    """Aborting an unknown id exits with a hard error before any write."""
    # Seed a real run so the DB exists, then abort a *different* id.
    _seed_run(db_path)

    bogus = "00000000-0000-0000-0000-000000000000"
    result = runner.invoke(
        app, ["run", "abort", bogus, "--db", str(db_path)]
    )
    assert result.exit_code != 0
    assert bogus in (result.stderr or "") or bogus in (result.output or "")
