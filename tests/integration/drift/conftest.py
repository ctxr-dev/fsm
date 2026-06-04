"""Shared fixtures for the long-LLM-friendliness drift detector tests.

These tests exercise the drift detector + MCP commit gate + auto-clear
policy end-to-end. The fixtures here build a real SQLite-backed
Project, expose a callable that seeds a minimal worker-bearing run,
and tear down the MCP module-global project handle so cases don't
leak bindings into one another.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import anyio
import pytest

from ctxr.fsm.core.models import (
    EventKind,
    FsmSpec,
    State,
    Transition,
    Worker,
)
from ctxr.fsm.mcp import _state as _mcp_state
from ctxr.fsm.sqlite import Project
from ctxr.fsm.sqlite.drift import (
    DRIFT_PRODUCER_KIND,
    DRIFT_PRODUCER_NAME,
    DriftConfig,
    RunScoreboard,
    _sweep_once,
)
from ctxr.fsm.sqlite.models_core import RunTable


@pytest.fixture
def project_factory():
    """Yield a callable that opens a Project on a per-test temp DB.

    The callable also binds the MCP module-global handle so the
    in-process tool calls in this module find their project. Teardown
    resets the handle so the next test starts clean.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fsm.sqlite3"

        def _open() -> Project:
            project = Project.open(db_path, migrate=True)
            _mcp_state.set_project(project)
            return project

        yield _open

        _mcp_state.reset_project()


def minimal_worker_spec(
    spec_id: str = "drift_friendly_demo",
    *,
    expected_max_wait_seconds: int | None = None,
) -> FsmSpec:
    """A two-state worker spec used by the long-LLM-friendliness tests.

    The entry state ``a`` carries a Worker so ``fsm.get_brief`` will
    emit ``worker_dispatched`` and the drift detector treats the
    state as having a non-empty worker for the per-state budget
    lookup.
    """
    return FsmSpec(
        id=spec_id,
        version=1,
        entry="a",
        states=[
            State(
                id="a",
                purpose="entry — worker-bearing state for drift tests",
                worker=Worker(
                    role="demo-worker",
                    prompt_template="say hi",
                    inputs=[],
                    expected_max_wait_seconds=expected_max_wait_seconds,
                ),
                transitions=[Transition(to="b", when="always")],
            ),
            State(id="b", purpose="terminal", transitions=[]),
        ],
    )


def seed_run(project: Project, *, spec: FsmSpec | None = None) -> str:
    """Register the spec, start a run, mark current_state, return run id.

    Mirrors the helper in the existing drift detector test suite —
    we poke ``current_state`` directly because ``Project.start_run``
    does not advance the engine.
    """
    spec = spec if spec is not None else minimal_worker_spec()
    registered = project.register_spec(spec)
    run = project.start_run(registered.spec.id, args={})
    with project.session_factory() as session, session.begin():
        row = session.get(RunTable, run.id)
        assert row is not None
        row.current_state = "a"
        session.add(row)
    return run.id


def emit_simple(
    project: Project,
    *,
    run_id: str,
    kind: EventKind,
    payload: dict | None = None,
) -> None:
    """Emit an event of arbitrary kind for ``run_id``."""
    with project.session_factory() as session, session.begin():
        producer = project.producers.upsert(
            session,
            kind="engine",
            name="test-engine",
        )
        project.events.emit(
            session,
            producer_id=producer.id,
            kind=kind.value,
            payload=payload or {},
            run_id=run_id,
        )


def emit_tool_call(
    project: Project,
    *,
    run_id: str,
    tool_name: str,
) -> None:
    """Emit a ``tool_call_observed`` event for ``tool_name``."""
    with project.session_factory() as session, session.begin():
        producer = project.producers.upsert(
            session,
            kind="agent",
            name="test-agent",
        )
        project.events.emit(
            session,
            producer_id=producer.id,
            kind=EventKind.tool_call_observed.value,
            payload={
                "tool_name": tool_name,
                "succeeded": True,
                "args_redacted": {},
            },
            run_id=run_id,
        )


def run_one_sweep(
    project: Project,
    *,
    config: DriftConfig | None = None,
    scoreboards: dict[str, RunScoreboard] | None = None,
) -> dict[str, RunScoreboard]:
    """Drive a single drift-detector sweep against ``project``.

    Returns the scoreboard map so the caller can keep state across
    sweeps (mirrors the loop's lifetime semantics).
    """
    cfg = config if config is not None else DriftConfig()
    sbs = scoreboards if scoreboards is not None else {}

    with project.session_factory() as session, session.begin():
        producer = project.producers.upsert(
            session,
            kind=DRIFT_PRODUCER_KIND,
            name=DRIFT_PRODUCER_NAME,
        )
    producer_id = producer.id

    async def _go() -> None:
        await _sweep_once(
            project,
            cfg=cfg,
            producer_id=producer_id,
            scoreboards=sbs,
        )

    anyio.run(_go)
    return sbs


def backdate_last_update(
    project: Project,
    *,
    run_id: str,
    iso_ts: str,
) -> None:
    """Rewrite ``runs.last_update_at`` directly for idle-detection tests."""
    with project.session_factory() as session, session.begin():
        row = session.get(RunTable, run_id)
        assert row is not None
        row.last_update_at = iso_ts
        session.add(row)
