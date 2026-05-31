"""HTTP integration tests for ``/api/v1/specs`` (W5 spec router).

These tests exercise the FastAPI routes in :mod:`ctxr.fsm.api.routes_specs`
end-to-end against a real SQLite-backed :class:`Project`. They cover the
three slug-level contracts called out in the W5 brief:

* ``POST /api/v1/specs`` registers a freshly-supplied FSM spec, runs
  schema + cross-cutting validation, and returns the
  :class:`SpecRegistered` envelope with ``created=True`` on first insert.
* ``GET /api/v1/specs`` lists every registered spec across every
  project as :class:`SpecSummary` rows, ordered by
  ``(project_slug, slug, version)``.
* ``GET /api/v1/specs/{slug}/versions`` lists every registered version
  for a given FSM id under the requested ``project_slug``.

Transport choice
----------------

The synchronous endpoints are driven via
:class:`fastapi.testclient.TestClient` — it handles the FastAPI
lifespan, the ``Depends(get_project)`` resolution, and the JSON
serialisation without an in-process uvicorn. SSE-style endpoints would
need the long-lived ``text/event-stream`` connection that ``TestClient``
cannot model cleanly; those tests live in a sibling module and spin up
a real ``uvicorn.Server`` in a thread.

Project handle binding
----------------------

Each test:

1. Opens a fresh :class:`Project` against a
   :class:`tempfile.TemporaryDirectory` SQLite path.
2. Binds it via :func:`ctxr.fsm.api._state.set_project` *before*
   constructing the :class:`TestClient`. The lifespan handler in
   :mod:`ctxr.fsm.api` notices a pre-bound project and leaves lifecycle
   ownership to the caller — mirroring the production boot sequence in
   :mod:`ctxr.fsm.api.server`.
3. Tears down by closing the project and calling
   :func:`_state.reset_project` so the module-level singleton does not
   leak between tests.

The :func:`bound_client` context manager wraps that lifecycle so each
test reads as a single ``with`` block with no boilerplate.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ctxr.fsm.api import _state, app
from ctxr.fsm.sqlite import Project

# ---------------------------------------------------------------------------
# Fixture spec definitions
# ---------------------------------------------------------------------------
#
# Two-state linear FSMs used as inputs to ``POST /api/v1/specs``. The
# shape matches what ``FsmSpec.model_validate`` accepts — we keep them
# as plain ``dict`` here so the test can submit the same JSON over the
# wire that a real client would, without round-tripping through
# Pydantic in the test harness itself.


def _make_spec(spec_id: str) -> dict[str, Any]:
    """Build a tiny two-state ``always``-guarded FSM definition.

    The shape is intentionally minimal — these tests target the spec
    registry router, not the engine or the predicate evaluator. Using
    ``always`` guards keeps validation focused on the schema +
    reachability checks the router actually runs.
    """
    return {
        "id": spec_id,
        "version": 1,
        "entry": "state_a",
        "states": [
            {
                "id": "state_a",
                "purpose": f"entry state for {spec_id}",
                "transitions": [{"to": "state_b", "when": "always"}],
            },
            {
                "id": "state_b",
                "purpose": f"terminal state for {spec_id}",
                "transitions": [],
            },
        ],
    }


def _make_spec_v2(spec_id: str) -> dict[str, Any]:
    """Build a second-version variant of :func:`_make_spec`.

    A different ``purpose`` string on the entry state changes the
    canonical hash so the registry mints version 2 instead of
    deduping. ``register_spec``'s version bump is hash-driven so any
    semantically observable change in the definition is enough.
    """
    spec = _make_spec(spec_id)
    spec["states"][0]["purpose"] = f"entry state for {spec_id} (v2)"
    return spec


# ---------------------------------------------------------------------------
# Bound-client helper
# ---------------------------------------------------------------------------


@contextmanager
def bound_client() -> Iterator[tuple[TestClient, Project]]:
    """Yield a ``(TestClient, Project)`` pair bound to a fresh DB.

    Pre-binding the project via :func:`_state.set_project` *before*
    constructing the ``TestClient`` is load-bearing: ``TestClient``
    enters the app's lifespan on first request, and the lifespan hook
    only opens its own project when none is already bound. Doing the
    bind first means the test owns the lifecycle (so it can close the
    project deterministically at teardown) instead of handing it off
    to the lifespan.

    Teardown unbinds the project *before* closing the engine so that
    any stray request mid-teardown raises a clean ``RuntimeError`` from
    ``_state.get_project`` instead of touching an already-disposed
    engine.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fsm.sqlite3"
        project = Project.open(db_path, migrate=True)
        _state.set_project(project)
        try:
            with TestClient(app) as client:
                yield client, project
        finally:
            _state.reset_project()
            project.close()


# ---------------------------------------------------------------------------
# Tests — POST /api/v1/specs
# ---------------------------------------------------------------------------


def test_post_specs_registers_new_spec() -> None:
    """``POST /api/v1/specs`` mints a fresh row on first submission.

    Confirms the happy-path contract:

    * 201 response with the :class:`SpecRegistered` envelope.
    * ``created=True`` (first registration, not a hash-dedup match).
    * ``version=1`` because the (project, slug) pair is brand new.
    * ``slug`` echoes the FSM id from the definition.
    * ``project_slug`` echoes the value submitted in the body.
    * ``hash`` is a 64-char hex string (SHA-256 canonical hash).
    * ``spec_id`` is the UUIDv7 row PK (36-char hyphenated string).
    """
    with bound_client() as (client, _project):
        response = client.post(
            "/api/v1/specs",
            json={
                "definition": _make_spec("api_register_demo"),
                "project_slug": "api_specs_demo",
            },
        )

    assert response.status_code == 201, response.text
    body = response.json()

    assert body["created"] is True
    assert body["slug"] == "api_register_demo"
    assert body["version"] == 1
    assert body["project_slug"] == "api_specs_demo"
    assert isinstance(body["hash"], str)
    assert len(body["hash"]) == 64
    assert isinstance(body["spec_id"], str)
    assert len(body["spec_id"]) == 36
    assert isinstance(body["project_id"], str)


def test_post_specs_dedups_byte_identical_resubmission() -> None:
    """Re-registering the same canonical spec returns ``created=False``.

    The registry is hash-keyed within ``(project_id, slug)``, so a
    byte-identical resubmission is a no-op insert: it returns the
    same ``spec_id``, the same ``hash``, the same ``version``, and
    ``created=False``. This is the idempotency guarantee the UI
    relies on when re-uploading a saved FSM.
    """
    definition = _make_spec("api_dedup_demo")
    with bound_client() as (client, _project):
        first = client.post(
            "/api/v1/specs",
            json={"definition": definition, "project_slug": "api_specs_demo"},
        )
        second = client.post(
            "/api/v1/specs",
            json={"definition": definition, "project_slug": "api_specs_demo"},
        )

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text

    first_body = first.json()
    second_body = second.json()

    assert first_body["created"] is True
    assert second_body["created"] is False
    assert second_body["spec_id"] == first_body["spec_id"]
    assert second_body["hash"] == first_body["hash"]
    assert second_body["version"] == first_body["version"] == 1


def test_post_specs_rejects_invalid_definition_with_422() -> None:
    """Schema-invalid definitions return 422 with a structured envelope.

    A missing ``entry`` field fails Pydantic's :class:`FsmSpec`
    validation; the router translates that into a 422 with a
    structured detail body so clients can render per-field
    complaints. We assert the envelope shape rather than the exact
    Pydantic error messages — those are an implementation detail of
    the validator and not part of the API contract.
    """
    broken = _make_spec("api_invalid_demo")
    del broken["entry"]
    with bound_client() as (client, _project):
        response = client.post(
            "/api/v1/specs",
            json={"definition": broken, "project_slug": "api_specs_demo"},
        )

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "invalid_spec_definition"
    assert isinstance(detail["errors"], list)
    assert len(detail["errors"]) >= 1


# ---------------------------------------------------------------------------
# Tests — GET /api/v1/specs
# ---------------------------------------------------------------------------


def test_get_specs_lists_every_registered_spec() -> None:
    """``GET /api/v1/specs`` returns every spec across every project.

    Registers two specs under one project plus a third spec under a
    different project, then asserts the listing contains all three
    in the canonical ordering ``(project_slug, slug, version)``.
    The full ``definition`` body must NOT appear in the listing (it
    is only carried by the detail endpoint) — we check the keys on
    each row explicitly to guard against accidental wire-shape
    drift.
    """
    with bound_client() as (client, _project):
        # Two specs in the "alpha" project (different slugs).
        client.post(
            "/api/v1/specs",
            json={
                "definition": _make_spec("alpha_spec_one"),
                "project_slug": "alpha",
            },
        ).raise_for_status()
        client.post(
            "/api/v1/specs",
            json={
                "definition": _make_spec("alpha_spec_two"),
                "project_slug": "alpha",
            },
        ).raise_for_status()
        # One spec in a separate project so the cross-project listing
        # behaviour is exercised.
        client.post(
            "/api/v1/specs",
            json={
                "definition": _make_spec("beta_spec_one"),
                "project_slug": "beta",
            },
        ).raise_for_status()

        response = client.get("/api/v1/specs")

    assert response.status_code == 200, response.text
    rows = response.json()
    assert isinstance(rows, list)
    assert len(rows) == 3

    # Canonical ordering: project_slug asc, slug asc, version asc.
    triples = [(row["project_slug"], row["slug"], row["version"]) for row in rows]
    assert triples == [
        ("alpha", "alpha_spec_one", 1),
        ("alpha", "alpha_spec_two", 1),
        ("beta", "beta_spec_one", 1),
    ]

    # Every row must carry the SpecSummary shape (no leakage of the
    # full definition body, which belongs on the detail endpoint).
    expected_keys = {
        "id",
        "project_id",
        "project_slug",
        "slug",
        "version",
        "hash",
        "created_at",
    }
    for row in rows:
        assert set(row.keys()) == expected_keys, (
            f"unexpected SpecSummary shape: {sorted(row.keys())!r}"
        )
        assert "definition" not in row


def test_get_specs_returns_empty_list_on_fresh_db() -> None:
    """A brand-new database returns ``[]`` from ``GET /api/v1/specs``.

    Important contract for the UI: the empty case must be a 200 with
    an empty array, not a 404 — the UI distinguishes "no specs yet"
    from "endpoint missing" by the status code, and a 404 here would
    flash a spurious error toast on first project load.
    """
    with bound_client() as (client, _project):
        response = client.get("/api/v1/specs")

    assert response.status_code == 200, response.text
    assert response.json() == []


# ---------------------------------------------------------------------------
# Tests — GET /api/v1/specs/{slug}/versions
# ---------------------------------------------------------------------------


def test_get_versions_lists_every_version_for_slug() -> None:
    """``GET /api/v1/specs/{slug}/versions`` lists every version.

    Registers two distinct versions of the same FSM ``id``, then
    asserts both come back ordered by ``version`` ascending. The
    second post mutates the entry-state purpose so the canonical
    hash changes — that hash change is what makes ``register_spec``
    mint version 2 instead of returning the dedup envelope.
    """
    slug = "versioned_demo"
    with bound_client() as (client, _project):
        v1 = client.post(
            "/api/v1/specs",
            json={"definition": _make_spec(slug), "project_slug": "alpha"},
        )
        v2 = client.post(
            "/api/v1/specs",
            json={"definition": _make_spec_v2(slug), "project_slug": "alpha"},
        )
        assert v1.status_code == 201
        assert v2.status_code == 201
        assert v1.json()["version"] == 1
        assert v2.json()["version"] == 2
        assert v1.json()["hash"] != v2.json()["hash"]

        response = client.get(
            f"/api/v1/specs/{slug}/versions",
            params={"project_slug": "alpha"},
        )

    assert response.status_code == 200, response.text
    rows = response.json()
    assert isinstance(rows, list)
    assert len(rows) == 2

    versions = [row["version"] for row in rows]
    assert versions == [1, 2], (
        f"versions must be ascending; got {versions!r}"
    )
    for row in rows:
        assert row["slug"] == slug
        assert row["project_slug"] == "alpha"


def test_get_versions_scopes_to_requested_project_slug() -> None:
    """Versions endpoint respects the ``project_slug`` query parameter.

    Registers the same FSM ``id`` under two different project slugs
    so the cross-project sharing of an FSM id is exercised. Each
    project's listing must include exactly one row — the version
    registered under that project, no leakage from the other.
    """
    slug = "shared_slug_demo"
    with bound_client() as (client, _project):
        client.post(
            "/api/v1/specs",
            json={"definition": _make_spec(slug), "project_slug": "alpha"},
        ).raise_for_status()
        client.post(
            "/api/v1/specs",
            json={"definition": _make_spec(slug), "project_slug": "beta"},
        ).raise_for_status()

        alpha_rows = client.get(
            f"/api/v1/specs/{slug}/versions",
            params={"project_slug": "alpha"},
        ).json()
        beta_rows = client.get(
            f"/api/v1/specs/{slug}/versions",
            params={"project_slug": "beta"},
        ).json()

    assert len(alpha_rows) == 1
    assert len(beta_rows) == 1
    assert alpha_rows[0]["project_slug"] == "alpha"
    assert beta_rows[0]["project_slug"] == "beta"
    # The two registrations have the same canonical hash (same
    # definition) but live under different projects, so the spec_id
    # differs.
    assert alpha_rows[0]["id"] != beta_rows[0]["id"]
    assert alpha_rows[0]["hash"] == beta_rows[0]["hash"]


def test_get_versions_returns_empty_for_unknown_slug() -> None:
    """Unknown ``slug`` returns ``[]``, not a 404.

    The router resolves unknown ``(project_slug, slug)`` pairs to an
    empty list — the UI uses the empty response to render a
    "no versions registered yet" affordance, which would break if
    the endpoint 404-ed instead.
    """
    with bound_client() as (client, _project):
        response = client.get(
            "/api/v1/specs/never_registered/versions",
            params={"project_slug": "default"},
        )

    assert response.status_code == 200, response.text
    assert response.json() == []


def test_get_versions_returns_empty_for_unknown_project_slug() -> None:
    """Unknown ``project_slug`` also returns ``[]`` (not a 404).

    Mirrors the unknown-slug case: the router checks for the project
    row first and short-circuits with an empty list when it is
    missing. This keeps the contract for "no rows" identical
    regardless of which half of the ``(project_slug, slug)`` pair is
    unrecognised.
    """
    with bound_client() as (client, _project):
        # Register at least one spec under a different project so the
        # database is not empty — we want to be sure the empty
        # response is project-scoped, not "the DB is blank".
        client.post(
            "/api/v1/specs",
            json={
                "definition": _make_spec("present_demo"),
                "project_slug": "present_project",
            },
        ).raise_for_status()

        response = client.get(
            "/api/v1/specs/present_demo/versions",
            params={"project_slug": "absent_project"},
        )

    assert response.status_code == 200, response.text
    assert response.json() == []


# ---------------------------------------------------------------------------
# Pytest marker plumbing
# ---------------------------------------------------------------------------
# Surface the "integration" marker tag clearly under ``pytest -m
# integration``. We register it via :func:`pytestmark` here (rather than
# in ``pyproject.toml``) so the suite still runs under
# ``--strict-markers`` until the project-wide marker registration lands.

pytestmark = pytest.mark.filterwarnings(
    "ignore::pytest.PytestUnknownMarkWarning"
)
