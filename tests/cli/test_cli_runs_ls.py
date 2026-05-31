"""CLI smoke tests for ``ctxr-fsm runs ls``.

The ``runs ls`` command is the cross-run listing endpoint. These tests
exercise it through Typer's :class:`CliRunner` (so we go through the
real Click/Typer argument parsing, exit-code handling, and option
precedence machinery) but populate the database via the Python API
(``Project.register_spec`` / ``Project.start_run``) so we never depend
on a second CLI command being correct in order to test this one.

Three contracts under test:

* On a brand-new (empty) database, ``runs ls --json`` returns the JSON
  literal ``[]`` and exits zero. No "no runs found" pretty message
  bleeds into the JSON channel.
* After registering a spec and starting a run via the Python API,
  ``runs ls --json`` surfaces the run in the JSON payload — id, status,
  spec id all match.
* ``--status`` filters to the requested status: an ``in_progress`` run
  appears under ``--status in_progress`` and disappears under
  ``--status completed``.

Each test uses a per-test :class:`tempfile.TemporaryDirectory` for the
project DB so the suite stays hermetic — no shared state between cases
and nothing left behind on disk after the test finishes.
"""

from __future__ import annotations

import json
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from ctxr.fsm.cli import app
from ctxr.fsm.sqlite import Project

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_fixture_spec_module(tmp_dir: Path) -> str:
    """Write a tiny ``fixture_spec`` Python module into ``tmp_dir``.

    Returns the module name (always ``"fixture_spec"``) so callers can
    import it after inserting ``tmp_dir`` onto ``sys.path``. The module
    declares a minimal two-state :class:`FsmSpec` named ``spec`` —
    enough to register and start a run against, no engine execution
    required.
    """
    module_path = tmp_dir / "fixture_spec.py"
    module_path.write_text(
        textwrap.dedent(
            """
            from ctxr.fsm import FsmSpec, State, Transition

            spec = FsmSpec(
                id="cli_runs_ls_test",
                version=1,
                entry="a",
                states=[
                    State(id="a", transitions=[Transition(to="b", when="always")]),
                    State(id="b"),
                ],
            )
            """
        ).strip()
        + "\n"
    )
    return "fixture_spec"


def _load_spec_from_tmp(tmp_dir: Path) -> Any:
    """Import the fixture spec module from ``tmp_dir`` and return the spec.

    Inserts ``tmp_dir`` at the front of ``sys.path`` so the import
    resolves to the file we just wrote rather than any stale entry from
    a prior test. We never remove the entry: ``sys.path`` is process-
    wide and the path itself disappears with the
    :class:`tempfile.TemporaryDirectory` block, so a stale entry would
    just fail to resolve in a later test — that's safer than mutating
    ``sys.path`` mid-test and racing with importlib's caches.
    """
    module_name = _write_fixture_spec_module(tmp_dir)
    sys.path.insert(0, str(tmp_dir))
    # Drop any cached version from a prior tempdir so we re-read the
    # file we just wrote rather than serving a stale spec.
    sys.modules.pop(module_name, None)
    import importlib

    module = importlib.import_module(module_name)
    return module.spec


def _run_cli(args: list[str]) -> Any:
    """Invoke the top-level Typer app with ``args`` via :class:`CliRunner`.

    Centralising the call site means the assertion sites stay focused
    on inputs/outputs rather than ``CliRunner`` boilerplate.
    """
    return runner.invoke(app, args)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_runs_ls_json_empty_db_returns_empty_list() -> None:
    """An empty project DB prints exactly the JSON literal ``[]``.

    The migration is run by ``open_project_for_cli`` (``migrate=True``)
    so the DB exists and the schema is current — but no runs have been
    started, so ``RunsRepo.latest`` returns an empty list and the
    ``--json`` path must round-trip that as ``[]`` (not ``null``, not
    a "no runs found" pretty banner).
    """
    with tempfile.TemporaryDirectory() as tmp_str:
        db_path = Path(tmp_str) / "fsm.db"

        result = _run_cli(["runs", "ls", "--db", str(db_path), "--json"])

        assert result.exit_code == 0, (
            f"runs ls exited {result.exit_code}: stdout={result.stdout!r}"
        )
        payload = json.loads(result.stdout)
        assert payload == [], f"expected [] on empty DB, got {payload!r}"


def test_runs_ls_json_shows_started_run() -> None:
    """After Python-API ``start_run``, ``runs ls --json`` surfaces the run.

    We deliberately register + start via the Python API (not the CLI
    ``spec register`` / hypothetical ``run start``) so the test isolates
    the ``runs ls`` contract from any other command's correctness.
    """
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp_dir = Path(tmp_str)
        db_path = tmp_dir / "fsm.db"

        # Seed the DB via the Python facade.
        spec = _load_spec_from_tmp(tmp_dir)
        with Project.open(db_path, migrate=True) as project:
            registered = project.register_spec(spec)
            run = project.start_run(registered.spec.id, args={"hello": "world"})
            expected_run_id = run.id
            expected_spec_id = registered.spec.id

        # Now drive the CLI.
        result = _run_cli(["runs", "ls", "--db", str(db_path), "--json"])

        assert result.exit_code == 0, (
            f"runs ls exited {result.exit_code}: stdout={result.stdout!r}"
        )
        payload = json.loads(result.stdout)
        assert isinstance(payload, list)
        assert len(payload) == 1, (
            f"expected exactly one run in the listing, got {payload!r}"
        )
        row = payload[0]
        assert row["id"] == expected_run_id
        assert row["fsm_spec_id"] == expected_spec_id
        # Freshly-started runs land in ``in_progress`` — the start_run
        # facade does not advance the state machine, so the status
        # never moves past the initial value.
        assert row["status"] == "in_progress"


def test_runs_ls_status_filter() -> None:
    """``--status`` scopes the listing to runs in the requested status.

    The newly-started run is ``in_progress`` so it appears under
    ``--status in_progress`` and is absent under ``--status completed``.
    Asserting both directions catches the easy bug where the filter is
    silently dropped (everything always passes through) as well as the
    opposite bug where the filter rejects everything (nothing ever
    passes through).
    """
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp_dir = Path(tmp_str)
        db_path = tmp_dir / "fsm.db"

        spec = _load_spec_from_tmp(tmp_dir)
        with Project.open(db_path, migrate=True) as project:
            registered = project.register_spec(spec)
            run = project.start_run(registered.spec.id)
            expected_run_id = run.id

        # --- positive: the in_progress filter finds our run ---
        match_result = _run_cli(
            [
                "runs",
                "ls",
                "--db",
                str(db_path),
                "--status",
                "in_progress",
                "--json",
            ]
        )
        assert match_result.exit_code == 0, (
            f"runs ls --status in_progress exited {match_result.exit_code}: "
            f"stdout={match_result.stdout!r}"
        )
        match_payload = json.loads(match_result.stdout)
        assert isinstance(match_payload, list)
        assert len(match_payload) == 1
        assert match_payload[0]["id"] == expected_run_id
        assert match_payload[0]["status"] == "in_progress"

        # --- negative: a status the run does not have yields [] ---
        miss_result = _run_cli(
            [
                "runs",
                "ls",
                "--db",
                str(db_path),
                "--status",
                "completed",
                "--json",
            ]
        )
        assert miss_result.exit_code == 0, (
            f"runs ls --status completed exited {miss_result.exit_code}: "
            f"stdout={miss_result.stdout!r}"
        )
        miss_payload = json.loads(miss_result.stdout)
        assert miss_payload == [], (
            f"expected [] when filtering by an unmatched status, got {miss_payload!r}"
        )
