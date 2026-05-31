"""Integration coverage for the always-on API endpoints.

The three endpoints exercised here are the API's "is the server alive
and pointed at a project?" trio:

* ``GET /healthz`` — liveness probe; does not touch the database, so
  it must return ``{"status": "ok"}`` even before the lifespan handler
  has bound a project.
* ``GET /readyz`` — readiness probe; returns 200 in both ready and
  starting states but the body distinguishes them via
  ``project_open``.
* ``GET /api/v1/projects/current`` — minimal project metadata; the
  first auth-guarded route, used by the UI as a "did my token actually
  work?" smoke probe.

These tests run inside :class:`fastapi.testclient.TestClient`, which
drives the ASGI app in-process and handles the FastAPI lifespan +
``Depends`` plumbing without spinning up uvicorn. SSE endpoints
(streaming) require a real socket and live in a separate test module
that spawns a programmatic uvicorn server in a thread — none of the
endpoints here stream, so the in-process client is the right tool.

Why the manual ``_state.set_project`` dance instead of letting the
lifespan hook open the DB itself? Two reasons:

1. We want every test to point at its own throwaway SQLite file under
   :class:`tempfile.TemporaryDirectory`, not the project-default
   ``./.ctxr-fsm/fsm.db`` that the lifespan would resolve to.
2. Pre-binding the project lets the lifespan handler take its "already
   open" branch (see :func:`ctxr.fsm.api.lifespan_handler`), which is
   the canonical production path that the ``ctxr-fsm api`` entry
   point also exercises. Tests staying on that path catch regressions
   in the "pre-bound" branch that the alternate "lifespan-opens-it"
   branch would silently hide.

The teardown ``_state.reset_project()`` keeps the module-global from
leaking across tests — pytest does not isolate module-level state by
default, so a forgotten reset would surface as flakiness in whichever
test happened to run next.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import ctxr.fsm
from ctxr.fsm.api import _state, app
from ctxr.fsm.sqlite import Project

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def project_and_client() -> Iterator[tuple[Project, TestClient, Path]]:
    """Yield a fresh ``(Project, TestClient, db_path)`` triple per test.

    Each test gets:

    * Its own :class:`tempfile.TemporaryDirectory` so the SQLite file
      cannot collide with another test or with a leftover from a prior
      run.
    * A :class:`Project` opened with ``migrate=True`` against that
      file, so the Alembic head is applied (mirroring the production
      boot sequence the API server uses).
    * The module-global API project handle bound to that project via
      :func:`_state.set_project` *before* :class:`TestClient` is
      constructed — this drives the lifespan handler's "already open"
      branch, which is the same branch the ``ctxr-fsm api`` entry
      point exercises in production.
    * A :class:`TestClient` instance that — entered as a context
      manager — runs the FastAPI lifespan startup hook so the app is
      fully wired by the time the test body issues its first request.

    Teardown unwinds in strict reverse order: close the client (runs
    the lifespan shutdown hook, which is a no-op when the project was
    pre-bound), reset the module-global handle, then close the
    project. Wrapping the client open/close in this fixture (rather
    than expecting the test body to do it) keeps the test bodies free
    of boilerplate.
    """
    # The temp dir auto-cleans on context-manager exit, taking the
    # SQLite file and Alembic ``_journal`` file with it; using
    # :class:`tempfile.TemporaryDirectory` directly (rather than
    # ``tmp_path``) keeps the fixture self-contained and explicit
    # about its lifecycle.
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "fsm.db"
        project = Project.open(db_path, migrate=True)
        _state.set_project(project)
        try:
            # ``with TestClient(app) as client`` is the documented way
            # to drive the FastAPI lifespan in tests — without the
            # ``with`` the lifespan hook never fires and any code that
            # relies on it (admin routers, future startup wiring) sees
            # a half-initialised app.
            with TestClient(app) as client:
                yield project, client, db_path
        finally:
            # Order matters: reset the module-global before close()
            # so any in-flight test code that introspects the handle
            # sees ``is_open() == False`` rather than a Project whose
            # engine has been disposed.
            _state.reset_project()
            project.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_healthz_returns_status_ok(
    project_and_client: tuple[Project, TestClient, Path],
) -> None:
    """``GET /healthz`` returns 200 with ``{"status": "ok"}``.

    Liveness must not depend on the project being open — kubernetes /
    systemd / docker-healthcheck callers do not present credentials
    and a 401 here would kick the pod into a restart loop. Asserting
    on the exact body shape (``{"status": "ok"}``) catches any future
    accidental field rename in :class:`HealthResponse`.
    """
    _, client, _ = project_and_client

    response = client.get("/healthz")

    assert response.status_code == 200, (
        f"expected 200 from /healthz, got {response.status_code}; body={response.text!r}"
    )
    assert response.json() == {"status": "ok"}, (
        f"unexpected /healthz body: {response.json()!r}"
    )


def test_readyz_reports_project_open(
    project_and_client: tuple[Project, TestClient, Path],
) -> None:
    """``GET /readyz`` returns 200 with ``status='ok'`` when the project is bound.

    The readiness endpoint is intentionally 200-in-both-states (open
    vs. starting) and uses the body to distinguish — see the route's
    docstring for the rationale. With the fixture's pre-bound
    project, we should observe ``status='ok'`` and
    ``project_open=True``; any other shape means either the lifespan
    hook did not fire or :func:`_state.is_open` regressed.
    """
    _, client, _ = project_and_client

    response = client.get("/readyz")

    assert response.status_code == 200, (
        f"expected 200 from /readyz, got {response.status_code}; body={response.text!r}"
    )
    body = response.json()
    assert body == {"status": "ok", "project_open": True}, (
        f"unexpected /readyz body when project is open: {body!r}"
    )


def test_get_current_project_returns_metadata(
    project_and_client: tuple[Project, TestClient, Path],
) -> None:
    """``GET /api/v1/projects/current`` returns the open project's metadata.

    The endpoint is auth-guarded but the suite runs in dev mode
    (``CTXR_FSM_API_TOKEN`` unset), so we expect 200 without a
    bearer header. The body shape mirrors :class:`ProjectMetadata`:

    * ``fsm_version`` — the installed ``ctxr.fsm`` package version
      (compared against the literal :data:`ctxr.fsm.__version__` so a
      version bump that changes the value flows through the assertion
      automatically).
    * ``project_open`` — ``True`` whenever the dependency could
      resolve the handle (which it must have, otherwise the route
      would have raised 500 from :func:`_state.get_project`).
    * ``db_path`` — the SQLite path our fixture passed to
      :meth:`Project.open`. Compared via ``Path.resolve()`` because
      macOS aliases ``/tmp`` to ``/private/tmp`` and the SQLAlchemy
      URL canonicalises the path, so a raw string compare would fail
      on darwin.
    """
    _, client, db_path = project_and_client

    response = client.get("/api/v1/projects/current")

    assert response.status_code == 200, (
        f"expected 200 from /api/v1/projects/current, got "
        f"{response.status_code}; body={response.text!r}"
    )

    body = response.json()
    assert body.get("fsm_version") == ctxr.fsm.__version__, (
        f"fsm_version mismatch: expected {ctxr.fsm.__version__!r}, "
        f"got {body.get('fsm_version')!r}"
    )
    assert body.get("project_open") is True, (
        f"expected project_open=True (the fixture pre-binds a Project); "
        f"body={body!r}"
    )

    reported_db = body.get("db_path")
    assert isinstance(reported_db, str) and reported_db, (
        f"expected a non-empty db_path string; body={body!r}"
    )
    # ``Path.resolve()`` on both sides normalises macOS's
    # /tmp -> /private/tmp aliasing and any other symlink quirks
    # the host OS might inject. The SQLAlchemy URL the route reads
    # from carries the path SQLite opened (post-resolve), so we have
    # to resolve the fixture's side too for the compare to be honest.
    assert Path(reported_db).resolve() == db_path.resolve(), (
        f"reported db_path {reported_db!r} does not match fixture path "
        f"{db_path!s} (resolved: {db_path.resolve()!s})"
    )


if __name__ == "__main__":  # pragma: no cover - manual debug path
    # Allow ``python tests/integration/api/test_api_health.py`` to
    # run the suite under pytest without remembering the full module
    # path. Handy when iterating on the test body locally.
    raise SystemExit(pytest.main([__file__, "-v"]))
