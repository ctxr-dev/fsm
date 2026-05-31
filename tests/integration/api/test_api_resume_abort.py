"""Integration coverage for the W5 run-lifecycle HTTP endpoints.

The three endpoints exercised here mirror the W4 MCP tools that own
the same operator hatches:

* ``POST /api/v1/runs/{run_id}/abort`` — flips a non-terminal run's
  status to ``aborted``, records ``ended_at``, and emits the
  ``run_aborted`` event for downstream subscribers. Field-for-field
  parallel of ``fsm.abort_run``.
* ``POST /api/v1/runs/{run_id}/journal/discard`` — deletes the
  newest unfinalised journal txn for a run (the rollback path).
  Parallel of ``fsm.recover_journal action=discard``.
* ``POST /api/v1/runs/{run_id}/journal/replay`` — flips a
  ``ready_to_finalise`` journal txn to ``finalised`` (the
  roll-forward path; the engine itself re-materialises staged
  writes on next boot — W12). Parallel of
  ``fsm.recover_journal action=replay``.

Why :class:`fastapi.testclient.TestClient`?
-------------------------------------------

Every endpoint here is plain request/response — no SSE, no
long-lived streams — so the in-process ASGI driver is the right
tool. ``TestClient`` runs the lifespan startup / shutdown hooks for
us as a side-effect of being used as a context manager, which means
the same "pre-bind a project, then enter the client" dance the
production entry point uses also runs in tests; the lifespan
handler's "already open" branch is the canonical path and these
tests stay on it.

Why pre-seed the project via :func:`_state.set_project`?
--------------------------------------------------------

Two reasons, both inherited from ``test_api_health.py``:

1. We want each test to point at its own throwaway SQLite file
   under :class:`tempfile.TemporaryDirectory` so the journal-row
   contents one test seeds cannot bleed into another, and so the
   default ``./.ctxr-fsm/fsm.db`` that the lifespan would otherwise
   resolve to is never touched.
2. Binding the project before constructing the :class:`TestClient`
   drives the lifespan handler's "already bound" branch, which is
   the production path the ``ctxr-fsm api`` entry point exercises.
   Tests staying on that branch catch regressions the alternate
   "lifespan-opens-it" branch would mask.

Teardown order matters: reset the module-global *before* closing
the project so any code that inspects ``_state.is_open()`` mid-tear
sees ``False`` instead of a live handle pointing at a disposed
engine. The fixture mirrors the contract documented in
``test_api_health.py`` so a contributor familiar with one file can
navigate the other without surprise.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ctxr.fsm.api import _state, app
from ctxr.fsm.core.models import EventKind, FsmSpec, State, Transition
from ctxr.fsm.sqlite import Project

# ---------------------------------------------------------------------------
# Spec + seed helpers
# ---------------------------------------------------------------------------


def _make_spec(spec_id: str = "api_resume_abort_demo") -> FsmSpec:
    """Return the minimal two-state FSM every test in this module uses.

    Shape: ``a`` (entry) → ``b`` (terminal). The cross-cutting
    structural validator that runs inside :meth:`Project.register_spec`
    refuses isolated states, so even though none of these tests
    actually drive a transition we still wire one ``always`` edge so
    registration succeeds.
    """
    return FsmSpec(
        id=spec_id,
        version=1,
        entry="a",
        states=[
            State(
                id="a",
                purpose="entry state",
                transitions=[Transition(to="b", when="always")],
            ),
            State(
                id="b",
                purpose="terminal state",
                transitions=[],
            ),
        ],
    )


def _seed_run(project: Project) -> str:
    """Register the demo spec on ``project`` and mint a fresh run.

    Returns the new run id. Splitting the seed into a helper keeps
    the test bodies free of the spec / start_run boilerplate and
    makes the "we want a run row to act on" intent obvious at the
    call site.
    """
    registered = project.register_spec(_make_spec())
    run = project.start_run(spec_id=registered.spec.id, args={})
    return run.id


def _seed_pending_journal(project: Project, run_id: str) -> str:
    """Open a ``pending`` journal txn against ``run_id``; return its id.

    Used by the ``journal/discard`` test: the pre-state for that
    endpoint is "a fresh pending row exists", and the post-state is
    "no unfinalised row exists" — so we need both the id (for the
    pre-state assertion) and the run id (for the route's URL).
    """
    with project.session_factory() as session, session.begin():
        txn = project.journal.open(session, run_id=run_id)
        return txn.id


def _seed_ready_journal(project: Project, run_id: str) -> str:
    """Open + mark_ready a journal txn so its status is ``ready_to_finalise``.

    Used by the ``journal/replay`` test: only ``ready_to_finalise``
    rows are legal to roll forward, so the seed walks the lifecycle
    ``open() → mark_ready()`` before handing the id back. The
    staged-writes payload is a single trivial entry — enough to
    exercise the JSON-encoding path without exploding the test
    surface with FSM-specific shapes.
    """
    with project.session_factory() as session, session.begin():
        txn = project.journal.open(session, run_id=run_id)
        project.journal.mark_ready(
            session,
            txn_id=txn.id,
            staged_writes=[{"kind": "demo", "payload": {"ok": True}}],
        )
        return txn.id


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def project_and_client() -> Iterator[tuple[Project, TestClient, Path]]:
    """Yield a fresh ``(Project, TestClient, db_path)`` triple per test.

    Mirrors the ``test_api_health.py`` fixture verbatim so the two
    files share the same lifecycle contract:

    * one :class:`tempfile.TemporaryDirectory` per test so SQLite
      files cannot collide;
    * a :class:`Project` opened with ``migrate=True`` so the Alembic
      head is applied (matching the production boot sequence);
    * the module-global API project handle bound *before* the
      :class:`TestClient` is constructed so the lifespan handler
      takes its "already open" branch;
    * teardown unwinds in strict reverse order — reset the
      module-global, then close the project — so any introspection
      mid-tear sees a clean state rather than a half-disposed engine.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "fsm.db"
        project = Project.open(db_path, migrate=True)
        _state.set_project(project)
        try:
            # ``with TestClient(app)`` triggers the FastAPI lifespan
            # startup hook; without the ``with`` the hook never fires
            # and code that relies on it (routers wired at app
            # construction, future startup tasks) sees a
            # half-initialised app.
            with TestClient(app) as client:
                yield project, client, db_path
        finally:
            _state.reset_project()
            project.close()


# ---------------------------------------------------------------------------
# POST /api/v1/runs/{run_id}/abort
# ---------------------------------------------------------------------------


def test_abort_flips_run_status_to_aborted(
    project_and_client: tuple[Project, TestClient, Path],
) -> None:
    """``POST /runs/{id}/abort`` flips the run's status to ``aborted``.

    Flow:

    1. Seed a fresh run; sanity-check it begins in ``in_progress``
       with no ``ended_at`` so the post-state delta is unambiguous.
    2. ``POST`` to the abort endpoint with an operator-supplied
       ``reason`` so the audit-trail field round-trips into both the
       response body and the persisted ``run_aborted`` event payload.
    3. Assert the response body (``AbortResult`` shape — ``run_id``,
       ``previous_status``, ``new_status='aborted'``, ``ended_at``,
       ``reason``) before re-opening the project handle.
    4. Re-read the run row through the public :class:`Project`
       façade and confirm the manifest now carries
       ``status='aborted'`` and a non-empty ``ended_at`` that
       matches the response field byte-for-byte.
    5. Walk the event log via :meth:`RunsRepo.events` and confirm a
       single ``run_aborted`` event was emitted *as the last event*
       (the seed's ``run_started`` is still first) and that its
       payload echoes the reason / previous status / ended_at.

    Why re-read through the project handle the fixture already holds
    rather than re-opening the DB file? The fixture's project is the
    same instance the API mutated, so a fresh ``runs.get`` shows the
    committed row without any extra file-lock dance — the inner
    ``with session.begin()`` block inside the route already
    committed the write.
    """
    project, client, _ = project_and_client
    run_id = _seed_run(project)

    # Pre-state sanity check: the run is in_progress, no ended_at.
    pre_run = project.get_run(run_id)
    assert pre_run is not None
    assert pre_run.status == "in_progress"
    assert pre_run.ended_at is None

    response = client.post(
        f"/api/v1/runs/{run_id}/abort",
        json={"reason": "user cancelled"},
    )

    assert response.status_code == 200, (
        f"expected 200 from abort, got {response.status_code}; "
        f"body={response.text!r}"
    )
    body = response.json()
    assert body["run_id"] == run_id
    assert body["previous_status"] == "in_progress"
    assert body["new_status"] == "aborted"
    assert body["reason"] == "user cancelled"
    # ``ended_at`` must be a non-empty ISO-8601 string — we don't
    # parse it (the timestamp format is the SQLite layer's contract
    # and is asserted on by lower-level tests) but we do require it
    # to be present so the audit trail is complete.
    assert isinstance(body["ended_at"], str) and body["ended_at"]

    # ── Manifest reflects the abort ───────────────────────────────
    post_run = project.get_run(run_id)
    assert post_run is not None
    assert post_run.status == "aborted"
    assert post_run.ended_at == body["ended_at"], (
        "response ended_at must match the persisted manifest value"
    )

    # ── Event log carries exactly one run_aborted, as the last event ─
    with project.session_factory() as session:
        events = list(project.runs.events(session, run_id))
    kinds = [event.kind for event in events]
    assert kinds.count(EventKind.run_aborted.value) == 1, (
        f"expected exactly one run_aborted event; got {kinds!r}"
    )
    assert kinds[-1] == EventKind.run_aborted.value, (
        f"expected run_aborted last; got {kinds!r}"
    )
    # The very first event is still the run_started emitted by
    # :meth:`Project.start_run` during seeding.
    assert kinds[0] == EventKind.run_started.value

    last_payload = dict(events[-1].payload)
    assert last_payload["run_id"] == run_id
    assert last_payload["reason"] == "user cancelled"
    assert last_payload["previous_status"] == "in_progress"
    assert last_payload["ended_at"] == body["ended_at"]


# ---------------------------------------------------------------------------
# POST /api/v1/runs/{run_id}/journal/discard
# ---------------------------------------------------------------------------


def test_journal_discard_removes_pending_txn(
    project_and_client: tuple[Project, TestClient, Path],
) -> None:
    """``POST /runs/{id}/journal/discard`` removes a pending journal txn.

    Flow:

    1. Seed a fresh run and open a ``pending`` journal txn against
       it. Sanity-check :meth:`JournalRepo.inspect` returns that
       exact row before the route runs.
    2. ``POST`` to ``/journal/discard``. The endpoint accepts no
       body — the action verb is in the URL.
    3. Assert the response shape (``JournalRecovered``): ``acted``
       is ``True``, ``action`` is ``discard``, ``txn_id`` echoes
       the seeded id, ``note`` is a non-empty diagnostic.
    4. Re-poll :meth:`JournalRepo.inspect`; it must return ``None``
       — the post-state for discard is "no unfinalised row exists".

    Why not assert on the exact ``note`` string? The note is an
    operator-facing diagnostic whose wording can evolve without
    breaking the contract; locking it down would make every prose
    tweak require a test edit. We assert non-empty + diagnostic
    presence and leave the wording to the route's docstring.
    """
    project, client, _ = project_and_client
    run_id = _seed_run(project)
    txn_id = _seed_pending_journal(project, run_id)

    # Pre-state sanity check: the pending row exists with the
    # expected id and status.
    with project.session_factory() as session:
        pre = project.journal.inspect(session, run_id=run_id)
    assert pre is not None
    assert pre.id == txn_id
    assert pre.status == "pending"

    response = client.post(f"/api/v1/runs/{run_id}/journal/discard")

    assert response.status_code == 200, (
        f"expected 200 from journal/discard, got {response.status_code}; "
        f"body={response.text!r}"
    )
    body = response.json()
    assert body["run_id"] == run_id
    assert body["action"] == "discard"
    assert body["acted"] is True, (
        f"expected acted=True after discarding a pending txn; body={body!r}"
    )
    assert body["txn_id"] == txn_id, (
        f"expected txn_id to echo the seeded txn {txn_id!r}; body={body!r}"
    )
    assert isinstance(body["note"], str) and body["note"], (
        f"expected a non-empty operator-facing note; body={body!r}"
    )

    # ── Post-state: no unfinalised row exists for the run ─────────
    with project.session_factory() as session:
        post = project.journal.inspect(session, run_id=run_id)
    assert post is None, (
        f"journal row {txn_id!r} was not discarded; still present as "
        f"{post!r}"
    )


# ---------------------------------------------------------------------------
# POST /api/v1/runs/{run_id}/journal/replay
# ---------------------------------------------------------------------------


def test_journal_replay_finalises_ready_txn(
    project_and_client: tuple[Project, TestClient, Path],
) -> None:
    """``POST /runs/{id}/journal/replay`` finalises a ``ready_to_finalise`` txn.

    Flow:

    1. Seed a fresh run and walk a journal txn through ``open() →
       mark_ready()`` so the row's status is ``ready_to_finalise``
       and ``staged_writes`` is non-empty. Sanity-check
       :meth:`JournalRepo.inspect` reports the right status.
    2. ``POST`` to ``/journal/replay``. The endpoint accepts no
       body — the action verb is in the URL.
    3. Assert the response shape: ``acted=True``, ``action='replay'``,
       ``txn_id`` echoes the seeded id, ``note`` is a non-empty
       diagnostic.
    4. Re-poll :meth:`JournalRepo.inspect`. After replay the row is
       ``finalised``, which falls outside the
       ``(pending, ready_to_finalise)`` filter ``inspect`` applies,
       so the poll returns ``None`` — that absence is the post-state
       we assert.

    The W4 + W5 contract is that ``replay`` *only* transitions the
    txn's status; the engine itself re-materialises the staged
    writes on next boot (W12). This test pins the status-transition
    half of that contract; the engine half lives behind W12 and
    will pick up its own tests when that wave lands.
    """
    project, client, _ = project_and_client
    run_id = _seed_run(project)
    txn_id = _seed_ready_journal(project, run_id)

    # Pre-state sanity check: ready_to_finalise with the expected id.
    with project.session_factory() as session:
        pre = project.journal.inspect(session, run_id=run_id)
    assert pre is not None
    assert pre.id == txn_id
    assert pre.status == "ready_to_finalise"

    response = client.post(f"/api/v1/runs/{run_id}/journal/replay")

    assert response.status_code == 200, (
        f"expected 200 from journal/replay, got {response.status_code}; "
        f"body={response.text!r}"
    )
    body = response.json()
    assert body["run_id"] == run_id
    assert body["action"] == "replay"
    assert body["acted"] is True, (
        f"expected acted=True after replaying a ready_to_finalise txn; "
        f"body={body!r}"
    )
    assert body["txn_id"] == txn_id, (
        f"expected txn_id to echo the seeded txn {txn_id!r}; body={body!r}"
    )
    assert isinstance(body["note"], str) and body["note"], (
        f"expected a non-empty operator-facing note; body={body!r}"
    )

    # ── Post-state: the row is now ``finalised``, which falls
    # outside ``inspect``'s ``(pending, ready_to_finalise)`` filter,
    # so a re-poll returns ``None``. That absence is the load-bearing
    # signal that the status transition fired.
    with project.session_factory() as session:
        post = project.journal.inspect(session, run_id=run_id)
    assert post is None, (
        f"expected ``inspect`` to return None after replay finalised "
        f"the txn; got {post!r}"
    )


if __name__ == "__main__":  # pragma: no cover - manual debug path
    # Allow ``python tests/integration/api/test_api_resume_abort.py``
    # to run the suite under pytest without remembering the full
    # module path. Handy when iterating on the test body locally.
    raise SystemExit(pytest.main([__file__, "-v"]))
