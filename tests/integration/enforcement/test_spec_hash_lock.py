"""Integration tests for the W12 spec-hash lock.

The spec-hash lock (W12 layer-9 enforcement) refuses to advance a run
whose snapshot hash (captured at ``start_run``) no longer matches the
latest registered version under the same ``(project_id, slug)``. The
contract surfaces as a structured ``fsm_spec_changed`` error envelope
carrying both ``run_hash`` and ``current_hash`` so an operator can
diff the two values.

These tests drive the MCP tool *bodies* in-process — no subprocess
spawn, no MCP framing — so the runtime stays well under a second per
case while still exercising the full Project facade + W2 substrate
end-to-end (real SQLite file, real Pydantic validation, real
event-bus emits).

Coverage layout
---------------

* ``test_commit_after_spec_change_returns_fsm_spec_changed`` — start a
  run, re-register the spec under a *different* shape, then call
  ``fsm.commit_outputs`` and expect the ``fsm_spec_changed`` envelope.
* ``test_commit_without_spec_change_succeeds`` — start a run, do NOT
  touch the spec, call ``fsm.commit_outputs`` and expect the normal
  ``advanced`` :class:`CommitResult` (no lock fires).
* ``test_fsm_spec_changed_payload_carries_both_hashes`` — focused
  payload-shape assertion: ``run_hash`` matches the hash recorded at
  start, ``current_hash`` matches the latest registered version's
  hash, and both are non-empty strings of the canonical length.
"""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

import pytest

from ctxr.fsm.core import spec as _spec_module  # noqa: F401  (binds .hash/.validate)
from ctxr.fsm.core.models import FsmSpec, State, Transition
from ctxr.fsm.mcp import _state as _mcp_state
from ctxr.fsm.mcp.tools_runs import (
    CommitOutputsInput,
    fsm_commit_outputs,
)
from ctxr.fsm.sqlite import Project

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def project_factory():
    """Yield a callable that opens a Project on a per-test temp DB.

    The factory pattern lets each test open the project, register a
    spec, start a run, then optionally re-register the spec to
    simulate drift — all against one durable SQLite file in a
    tmpdir. The MCP module-global project handle is reset on teardown
    so in-process tool calls in the next test see a fresh binding.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fsm.sqlite3"

        def _open() -> Project:
            project = Project.open(db_path, migrate=True)
            _mcp_state.set_project(project)
            return project

        yield _open

        _mcp_state.reset_project()


def _two_state_spec(spec_id: str = "enforcement_demo") -> FsmSpec:
    """Build the minimal two-state spec used by the spec-hash tests.

    Shape: ``a`` (entry, ``always`` → ``b``) → ``b`` (terminal). No
    worker, no allowed_tools, no verifier — so the cosignature gate
    stays off and the spec-hash lock is the only enforcement that
    could fire on commit.
    """
    return FsmSpec(
        id=spec_id,
        version=1,
        entry="a",
        states=[
            State(
                id="a",
                purpose="entry",
                transitions=[Transition(to="b", when="always")],
            ),
            State(id="b", purpose="terminal", transitions=[]),
        ],
    )


def _two_state_spec_with_extra(spec_id: str = "enforcement_demo") -> FsmSpec:
    """Variant of :func:`_two_state_spec` with an added intermediate state.

    Adds a third state ``c`` so the canonical-JSON shape (and therefore
    ``FsmSpec.hash()``) differs from the original. Re-registering this
    variant under the same ``spec_id`` slug mints a new row whose hash
    is what the spec-hash lock compares the in-flight run's snapshot
    against.
    """
    return FsmSpec(
        id=spec_id,
        version=1,
        entry="a",
        states=[
            State(
                id="a",
                purpose="entry",
                transitions=[Transition(to="b", when="always")],
            ),
            State(
                id="b",
                purpose="middle",
                transitions=[Transition(to="c", when="always")],
            ),
            State(id="c", purpose="terminal", transitions=[]),
        ],
    )


# ---------------------------------------------------------------------------
# Spec-hash lock tests
# ---------------------------------------------------------------------------


def test_commit_after_spec_change_returns_fsm_spec_changed(
    project_factory,
) -> None:
    """Re-registering the spec under an in-flight run blocks commit_outputs.

    Flow:
    1. Register v1 + start a run (snapshot hash is captured against
       the run row).
    2. Re-register a *different* shape under the same slug — a new
       spec row is minted with a new hash.
    3. Call ``fsm.commit_outputs`` against the original run — the
       layer-9 spec-hash lock fires and returns the structured
       ``fsm_spec_changed`` envelope instead of advancing.
    """
    project = project_factory()
    try:
        # --- step 1: register v1 + start a run ---
        registered_v1 = project.register_spec(_two_state_spec())
        run = project.start_run(registered_v1.spec.id, args={"seed": "x"})

        # --- step 2: re-register a different shape under the same slug ---
        registered_v2 = project.register_spec(_two_state_spec_with_extra())
        assert registered_v2.created is True, (
            "the v2 shape must mint a new row so the hash differs"
        )
        assert registered_v2.spec.hash != registered_v1.spec.hash, (
            "v2's hash must differ from v1's for the lock to have anything "
            "to drift against"
        )

        # --- step 3: try to commit against the original run ---
        result = fsm_commit_outputs(
            CommitOutputsInput(
                run_id=uuid.UUID(run.id),
                outputs={"hello": "world"},
            )
        )

        assert hasattr(result, "error"), (
            f"expected an error envelope, got {result!r}"
        )
        assert result.error == "fsm_spec_changed", (
            f"expected error=fsm_spec_changed, got {result.error!r}"
        )
    finally:
        project.close()


def test_commit_without_spec_change_succeeds(project_factory) -> None:
    """When the spec has not changed, commit_outputs advances normally.

    Negative control for the spec-hash lock: if no re-registration has
    happened, the run's snapshot hash matches the latest registered
    version's hash and the commit goes through the normal engine path,
    producing an ``advanced`` :class:`CommitResult` with a non-empty
    ``next_state``.
    """
    project = project_factory()
    try:
        registered = project.register_spec(_two_state_spec())
        run = project.start_run(registered.spec.id, args={"seed": "x"})

        result = fsm_commit_outputs(
            CommitOutputsInput(
                run_id=uuid.UUID(run.id),
                outputs={"hello": "world"},
            )
        )

        assert not hasattr(result, "error"), (
            f"expected a successful CommitResult, got error envelope {result!r}"
        )
        # The two-state spec resolves ``always`` to ``b``; the W12
        # two-phase commit returns ``kind="advanced"`` with
        # ``next_state`` populated. The state-row update is staged in
        # the journal txn (confirm_commit replays it), but the result
        # surface is the same — no lock fired.
        assert result.kind == "advanced", (
            f"expected kind=advanced, got {result.kind!r}"
        )
        assert result.next_state == "b", (
            f"expected next_state=b, got {result.next_state!r}"
        )
        assert result.expected_next_state == "b"
        assert result.token is not None, (
            "successful commit must mint a CommitToken for confirm_commit"
        )
    finally:
        project.close()


def test_fsm_spec_changed_payload_carries_both_hashes(
    project_factory,
) -> None:
    """The ``fsm_spec_changed`` envelope's payload exposes both hashes.

    The W12 contract is that the structured error carries the run's
    snapshot hash (``run_hash``) and the hash of the latest registered
    version under the same slug (``current_hash``). Operators rely on
    both values to diff the spec, so we assert each is present,
    non-empty, and matches the value the test computed independently.
    """
    project = project_factory()
    try:
        registered_v1 = project.register_spec(_two_state_spec())
        run = project.start_run(registered_v1.spec.id, args={})
        expected_run_hash = registered_v1.spec.hash

        registered_v2 = project.register_spec(_two_state_spec_with_extra())
        expected_current_hash = registered_v2.spec.hash
        assert expected_current_hash != expected_run_hash, (
            "fixture sanity: the two specs must hash to different values"
        )

        result = fsm_commit_outputs(
            CommitOutputsInput(
                run_id=uuid.UUID(run.id),
                outputs={"hello": "world"},
            )
        )

        assert hasattr(result, "error"), (
            f"expected an error envelope, got {result!r}"
        )
        assert result.error == "fsm_spec_changed"

        # The payload is the per-error structured dict on
        # :class:`McpToolError`. Both hashes must be present and
        # populated with the canonical values computed above.
        assert result.payload is not None, (
            "fsm_spec_changed envelope must carry a payload dict"
        )
        assert "run_hash" in result.payload, (
            f"payload missing run_hash: {result.payload!r}"
        )
        assert "current_hash" in result.payload, (
            f"payload missing current_hash: {result.payload!r}"
        )
        assert result.payload["run_hash"] == expected_run_hash, (
            f"run_hash mismatch: expected {expected_run_hash!r}, "
            f"got {result.payload['run_hash']!r}"
        )
        assert result.payload["current_hash"] == expected_current_hash, (
            f"current_hash mismatch: expected {expected_current_hash!r}, "
            f"got {result.payload['current_hash']!r}"
        )
        # Both hashes are non-empty strings — clients render them
        # verbatim in error messages, so empty values would be a real
        # regression in the error envelope.
        assert isinstance(result.payload["run_hash"], str)
        assert isinstance(result.payload["current_hash"], str)
        assert result.payload["run_hash"]
        assert result.payload["current_hash"]
    finally:
        project.close()
