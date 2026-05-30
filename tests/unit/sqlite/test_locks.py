"""Unit tests for :class:`ctxr.fsm.sqlite.LocksRepo`.

Coverage:

* ``acquire`` on a free lock returns ``acquired=True`` plus a populated
  :class:`Lock` snapshot.
* A second ``acquire`` by a different session against a live lock returns
  ``acquired=False`` with ``reason="held"``.
* When the existing lease has expired, a different session can take over
  via ``acquire`` (``reason="replaced_stale"``).
* ``release`` with the wrong session id refuses (``reason="not_owner"``)
  and leaves the row intact.
* ``release`` with the right session id removes the row so a subsequent
  ``inspect`` returns ``None``.

Each test gets its own on-disk SQLite database under a per-test
``TemporaryDirectory`` to keep behaviour isolated.
"""

from __future__ import annotations

import tempfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from ctxr.fsm.core.models import FsmSpec, State
from ctxr.fsm.sqlite import Lock, LockResult, Project, ReleaseResult

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _build_spec() -> FsmSpec:
    """Return a minimal FsmSpec valid enough to register and start a run."""
    return FsmSpec(
        id="locks-test-spec",
        version=1,
        entry="start",
        states=[State(id="start", purpose="kickoff")],
    )


@pytest.fixture()
def project_with_run() -> Iterator[tuple[Project, str]]:
    """Yield (project, run_id) backed by a fresh on-disk SQLite database.

    A real run row is required because ``locks.run_id`` is a FOREIGN KEY
    into ``runs.id`` and the substrate runs with ``PRAGMA foreign_keys=ON``.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fsm.sqlite"
        project = Project.open(db_path)
        try:
            registered = project.register_spec(_build_spec())
            run = project.start_run(registered.spec.id)
            yield project, run.id
        finally:
            project.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_acquire_on_free_lock_returns_acquired_true(
    project_with_run: tuple[Project, str],
) -> None:
    """A first ``acquire`` against a free run claims the lock cleanly."""
    project, run_id = project_with_run
    session_id = str(uuid.uuid4())

    with project.session_factory() as session, session.begin():
        result = project.locks.acquire(
            session, run_id=run_id, session_id=session_id
        )

    assert isinstance(result, LockResult)
    assert result.acquired is True
    assert result.reason == "acquired"
    assert isinstance(result.lock, Lock)
    assert result.lock.run_id == run_id
    assert result.lock.holder_session_id == session_id
    # Fresh lease must not be stale.
    assert result.lock.is_stale is False


def test_second_acquire_with_different_session_is_refused(
    project_with_run: tuple[Project, str],
) -> None:
    """A live, foreign lock is not stealable via ``acquire``."""
    project, run_id = project_with_run
    holder = str(uuid.uuid4())
    intruder = str(uuid.uuid4())

    with project.session_factory() as session, session.begin():
        first = project.locks.acquire(
            session, run_id=run_id, session_id=holder, ttl_seconds=3600
        )
    assert first.acquired is True

    with project.session_factory() as session, session.begin():
        second = project.locks.acquire(
            session, run_id=run_id, session_id=intruder
        )

    assert second.acquired is False
    assert second.reason == "held"
    # The repo surfaces the existing lock so callers can show "held by X".
    assert second.lock is not None
    assert second.lock.holder_session_id == holder


def test_expired_lock_allows_takeover(
    project_with_run: tuple[Project, str],
) -> None:
    """A stale lease (expires_at <= now) is taken over by another session."""
    project, run_id = project_with_run
    holder = str(uuid.uuid4())
    successor = str(uuid.uuid4())

    # ttl_seconds=0 makes expires_at == acquired_at; any later "now" is then
    # strictly greater, so the lease counts as stale on the next acquire.
    with project.session_factory() as session, session.begin():
        initial = project.locks.acquire(
            session, run_id=run_id, session_id=holder, ttl_seconds=0
        )
    assert initial.acquired is True

    # Make absolutely sure wall-clock time advances past the expiry stamp
    # even on a fast machine where millisecond truncation could tie.
    time.sleep(0.01)

    with project.session_factory() as session, session.begin():
        takeover = project.locks.acquire(
            session, run_id=run_id, session_id=successor, ttl_seconds=3600
        )

    assert takeover.acquired is True
    assert takeover.reason == "replaced_stale"
    assert takeover.lock is not None
    assert takeover.lock.holder_session_id == successor

    # And the persisted row now reflects the new holder.
    with project.session_factory() as session:
        current = project.locks.inspect(session, run_id=run_id)
    assert current is not None
    assert current.holder_session_id == successor


def test_release_with_wrong_session_refuses(
    project_with_run: tuple[Project, str],
) -> None:
    """``release`` from a non-holder leaves the row intact."""
    project, run_id = project_with_run
    holder = str(uuid.uuid4())
    intruder = str(uuid.uuid4())

    with project.session_factory() as session, session.begin():
        project.locks.acquire(
            session, run_id=run_id, session_id=holder
        )

    with project.session_factory() as session, session.begin():
        result = project.locks.release(
            session, run_id=run_id, session_id=intruder
        )

    assert isinstance(result, ReleaseResult)
    assert result.released is False
    assert result.reason == "not_owner"

    # Row is still there, still owned by the original holder.
    with project.session_factory() as session:
        still = project.locks.inspect(session, run_id=run_id)
    assert still is not None
    assert still.holder_session_id == holder


def test_release_with_right_session_removes(
    project_with_run: tuple[Project, str],
) -> None:
    """``release`` from the holder deletes the row."""
    project, run_id = project_with_run
    holder = str(uuid.uuid4())

    with project.session_factory() as session, session.begin():
        project.locks.acquire(
            session, run_id=run_id, session_id=holder
        )

    with project.session_factory() as session, session.begin():
        result = project.locks.release(
            session, run_id=run_id, session_id=holder
        )

    assert result.released is True
    assert result.reason == "released"

    with project.session_factory() as session:
        gone = project.locks.inspect(session, run_id=run_id)
    assert gone is None

    # A subsequent release on an empty slot is idempotent: not_held.
    with project.session_factory() as session, session.begin():
        again = project.locks.release(
            session, run_id=run_id, session_id=holder
        )
    assert again.released is False
    assert again.reason == "not_held"
