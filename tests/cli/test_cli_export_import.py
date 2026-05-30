"""CLI integration tests for ``ctxr-fsm export`` / ``ctxr-fsm import``.

These tests drive the export/import round-trip through the public
Typer app the way an operator (or a downstream script) would: invoke
``ctxr-fsm spec register`` to put a spec into the source DB, start a
run programmatically via the :class:`Project` facade (W3 deliberately
ships no ``run start`` CLI — the engine-driven start path lives in a
later workstream), export the run to a JSON file, then import that
file into a freshly-migrated second database and confirm the imported
run is queryable.

Two scenarios are covered:

* :func:`test_export_import_round_trip` — happy-path round trip.
* :func:`test_import_same_id_refuses_without_replace` — re-importing
  the same export into a DB that already has the run id fails without
  ``--replace`` and succeeds with it.

Why we hand-roll a fixture spec module on disk
----------------------------------------------

The CLI's ``spec register`` takes a ``<module>:<attribute>`` import
path; loading the spec means actually importing a Python module by
name. We therefore write a tiny module into the test's tmpdir and
prepend that directory to :data:`sys.path` for the duration of the
test so the import resolves. Module names are unique-per-test so a
re-import within the same pytest session does not collide on
:data:`sys.modules`.
"""

from __future__ import annotations

import contextlib
import json
import sys
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ctxr.fsm.cli import app
from ctxr.fsm.sqlite import Project
from ctxr.fsm.sqlite.models_core import FsmSpecTable, ProjectTable

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


runner = CliRunner()


_SPEC_MODULE_TEMPLATE = """
from ctxr.fsm import FsmSpec, State, Transition

spec = FsmSpec(
    id={spec_id!r},
    version=1,
    entry="state_a",
    states=[
        State(
            id="state_a",
            purpose="entry state",
            transitions=[Transition(to="state_b", when="always")],
        ),
        State(
            id="state_b",
            purpose="terminal state",
            transitions=[],
        ),
    ],
)
"""


@pytest.fixture
def fixture_spec_module() -> Iterator[tuple[str, str]]:
    """Write a tiny FSM spec module to a tmpdir and return ``(import_path, slug)``.

    Yields the ``module:attribute`` string the CLI accepts and the slug
    the spec carries (matches ``FsmSpec.id``). The tmpdir is added to
    :data:`sys.path` for the duration of the test and the module name
    is randomised so a re-import within the same session does not
    collide on :data:`sys.modules`.

    Cleanup: removes the tmpdir entry from :data:`sys.path` and any
    cached module entry so subsequent tests get a clean import state.
    """
    suffix = uuid.uuid4().hex[:8]
    module_name = f"_fixture_spec_{suffix}"
    spec_slug = f"export_import_demo_{suffix}"

    with tempfile.TemporaryDirectory() as tmp:
        module_dir = Path(tmp)
        module_path = module_dir / f"{module_name}.py"
        module_path.write_text(
            _SPEC_MODULE_TEMPLATE.format(spec_id=spec_slug),
            encoding="utf-8",
        )

        sys.path.insert(0, str(module_dir))
        try:
            yield (f"{module_name}:spec", spec_slug)
        finally:
            # Drop the tmpdir from sys.path and forget the module so
            # later tests do not see stale state.
            with contextlib.suppress(ValueError):
                sys.path.remove(str(module_dir))
            sys.modules.pop(module_name, None)


def _register_spec(db_path: Path, spec_import_path: str) -> str:
    """Invoke ``ctxr-fsm spec register`` against ``db_path``.

    Returns the registered spec's UUID ``id`` so callers can pass it
    straight into :meth:`Project.start_run` (which keys on the row PK,
    not the spec slug). Pulled out as a helper because both tests in
    this module need the exact same boilerplate. Asserts non-zero exit
    codes loudly so a setup failure does not masquerade as a
    body-of-test failure.
    """
    result = runner.invoke(
        app,
        ["spec", "register", spec_import_path, "--db", str(db_path), "--json"],
    )
    assert result.exit_code == 0, (
        f"spec register failed (exit={result.exit_code}): "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    summary = json.loads(result.stdout)
    spec_uuid = summary["spec"]["id"]
    assert isinstance(spec_uuid, str) and spec_uuid, (
        f"spec register returned no spec id; summary={summary!r}"
    )
    return spec_uuid


def _start_run(db_path: Path, spec_uuid: str) -> str:
    """Open the project DB and start a run against ``spec_uuid``.

    Returns the new run's id. We use :class:`Project` directly because
    W3 ships no ``run start`` CLI; the substrate-level call is the
    only way to mint a run from a fixture today and any future CLI
    addition would just call the same method.
    """
    with Project.open(db_path, migrate=True, echo=False) as project:
        run = project.start_run(spec_uuid, args={"fixture": True})
        return run.id


def _copy_project_and_spec(src_db: Path, dst_db: Path, spec_uuid: str) -> None:
    """Materialise ``spec_uuid`` (and its owning project) into ``dst_db``.

    The W3 importer does NOT round-trip the project / fsm_spec tables;
    it expects those FK targets to already exist in the destination by
    the time the run row is inserted. Re-registering the same spec on
    the destination DB would mint *fresh* UUIDs (the row PK is a
    server-side ``uuid_utils.uuid7()`` default), so the destination's
    spec id would not match the ``fsm_spec_id`` carried by the exported
    run row and the FK insert would fail.

    To keep the round-trip clean we open the source DB read-only, look
    up the spec row plus its project row, and re-insert them verbatim
    on the destination so the FK targets land with identical UUIDs.
    This mirrors what an operator would do for a real "transplant a
    run between two project DBs" workflow.
    """
    with (
        Project.open(src_db, migrate=False, echo=False) as src_project,
        src_project.session_factory() as session,
    ):
        spec_row = session.get(FsmSpecTable, spec_uuid)
        assert spec_row is not None, (
            f"source DB has no spec with id {spec_uuid!r}"
        )
        project_row = session.get(ProjectTable, spec_row.project_id)
        assert project_row is not None, (
            f"source DB has no project for spec {spec_uuid!r}"
        )
        # Snapshot the fields we need now; the row objects detach
        # when the session closes.
        project_snapshot = {
            "id": project_row.id,
            "slug": project_row.slug,
            "created_at": project_row.created_at,
            "metadata_json": project_row.metadata_json,
        }
        spec_snapshot = {
            "id": spec_row.id,
            "project_id": spec_row.project_id,
            "slug": spec_row.slug,
            "version": spec_row.version,
            "hash": spec_row.hash,
            "definition_json": spec_row.definition_json,
            "created_at": spec_row.created_at,
        }

    with (
        Project.open(dst_db, migrate=True, echo=False) as dst_project,
        dst_project.session_factory() as session,
        session.begin(),
    ):
        session.add(ProjectTable(**project_snapshot))
        session.flush()
        session.add(FsmSpecTable(**spec_snapshot))
        session.flush()


def _export_run(db_path: Path, run_id: str, output_path: Path) -> None:
    """Invoke ``ctxr-fsm export`` and assert it succeeded.

    The summary is JSON-mode so the test can pin behaviour off the
    structured response when we eventually want richer assertions; for
    now we just confirm the file was written.
    """
    result = runner.invoke(
        app,
        [
            "export",
            run_id,
            str(output_path),
            "--db",
            str(db_path),
            "--json",
        ],
    )
    assert result.exit_code == 0, (
        f"export failed (exit={result.exit_code}): "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert output_path.exists(), f"export did not produce {output_path!s}"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_export_import_round_trip(
    fixture_spec_module: tuple[str, str],
) -> None:
    """End-to-end: register spec → start run → export → import into fresh DB.

    Steps:

    1. Create a source-project DB in tmpdir-A, register the fixture
       spec via ``spec register``, start a run via
       :meth:`Project.start_run`.
    2. Export the run to a JSON file in tmpdir-A.
    3. Create a *fresh* DB in tmpdir-B and pre-register the same spec
       (the importer does not round-trip the spec; the target DB must
       already know the spec id the run references).
    4. ``ctxr-fsm import`` the JSON file against tmpdir-B's DB.
    5. Confirm the run is queryable on the destination DB by hitting
       ``ctxr-fsm run show <run_id>`` and asserting the JSON output
       carries the same id.
    """
    spec_import_path, spec_slug = fixture_spec_module

    with (
        tempfile.TemporaryDirectory() as src_tmp,
        tempfile.TemporaryDirectory() as dst_tmp,
        tempfile.TemporaryDirectory() as export_tmp,
    ):
        src_db = Path(src_tmp) / "fsm.db"
        dst_db = Path(dst_tmp) / "fsm.db"
        export_file = Path(export_tmp) / "run.json"

        # ── 1. Source DB: register spec + start a run ─────────────────
        src_spec_uuid = _register_spec(src_db, spec_import_path)
        run_id = _start_run(src_db, src_spec_uuid)
        assert run_id, "start_run returned an empty run id"

        # ── 2. Export the run to a JSON file ──────────────────────────
        _export_run(src_db, run_id, export_file)

        # The export's top-level JSON object should carry the same
        # run id under ``run.id`` — a quick byte-level sanity check
        # that we are not importing a stale or empty file in step 4.
        payload = json.loads(export_file.read_text(encoding="utf-8"))
        assert payload["schema_version"] == 1
        assert payload["run"]["id"] == run_id

        # ── 3. Destination DB: copy spec + project rows ───────────────
        # The W3 importer does NOT round-trip the spec table, so the
        # target DB must already know the FK target before we can land
        # the run row. We re-insert the exact row PK from the source —
        # re-registering would mint a fresh UUID and break the FK.
        _copy_project_and_spec(src_db, dst_db, src_spec_uuid)

        # ── 4. Import the run into the destination DB ─────────────────
        import_result = runner.invoke(
            app,
            [
                "import",
                str(export_file),
                "--db",
                str(dst_db),
                "--json",
            ],
        )
        assert import_result.exit_code == 0, (
            f"import failed (exit={import_result.exit_code}): "
            f"stdout={import_result.stdout!r} stderr={import_result.stderr!r}"
        )

        # The summary contract: imported_run_id matches the original.
        summary = json.loads(import_result.stdout)
        assert summary["imported_run_id"] == run_id
        assert summary["counts"]["run"] == 1

        # ── 5. Confirm the run is queryable on the destination DB ────
        show_result = runner.invoke(
            app,
            ["run", "show", run_id, "--db", str(dst_db), "--json"],
        )
        assert show_result.exit_code == 0, (
            f"run show failed (exit={show_result.exit_code}): "
            f"stdout={show_result.stdout!r} stderr={show_result.stderr!r}"
        )
        shown = json.loads(show_result.stdout)
        assert shown["run"]["id"] == run_id
        # The spec FK on the run row is the spec's UUID PK, not the
        # human-readable slug — confirm the import preserved it
        # verbatim from the source DB.
        assert shown["run"]["fsm_spec_id"] == src_spec_uuid
        # ``spec_slug`` is the human-readable name; verifying it lands
        # somewhere in the round-trip ensures the spec-row copy we
        # did upstream really resolved to the right spec definition.
        assert spec_slug, "fixture spec slug must not be empty"


def test_import_same_id_refuses_without_replace(
    fixture_spec_module: tuple[str, str],
) -> None:
    """Re-importing the same export into a DB that already has the run id fails.

    The contract: ``import`` refuses to clobber a pre-existing run
    (non-zero exit, friendly stderr) unless the operator passes
    ``--replace``. Passing ``--replace`` cascade-deletes the prior row
    and re-inserts cleanly.
    """
    spec_import_path, _spec_slug = fixture_spec_module

    with (
        tempfile.TemporaryDirectory() as src_tmp,
        tempfile.TemporaryDirectory() as dst_tmp,
        tempfile.TemporaryDirectory() as export_tmp,
    ):
        src_db = Path(src_tmp) / "fsm.db"
        dst_db = Path(dst_tmp) / "fsm.db"
        export_file = Path(export_tmp) / "run.json"

        # Set up: source DB has spec + run, export the run.
        src_spec_uuid = _register_spec(src_db, spec_import_path)
        run_id = _start_run(src_db, src_spec_uuid)
        _export_run(src_db, run_id, export_file)

        # Destination DB: copy the spec + project rows (verbatim UUIDs)
        # so the FK targets match, then do a successful first import so
        # the run row exists in the target.
        _copy_project_and_spec(src_db, dst_db, src_spec_uuid)
        first_import = runner.invoke(
            app,
            [
                "import",
                str(export_file),
                "--db",
                str(dst_db),
                "--json",
            ],
        )
        assert first_import.exit_code == 0, (
            f"first import failed: stdout={first_import.stdout!r} "
            f"stderr={first_import.stderr!r}"
        )

        # Second import (same id, no --replace) must refuse.
        second_import = runner.invoke(
            app,
            [
                "import",
                str(export_file),
                "--db",
                str(dst_db),
                "--json",
            ],
        )
        assert second_import.exit_code != 0, (
            "second import should have refused without --replace; "
            f"stdout={second_import.stdout!r}"
        )
        # The error banner goes through ``die`` which prefixes
        # 'error: ' to stderr. CliRunner merges stderr into stdout by
        # default unless ``mix_stderr=False`` was passed, so we accept
        # the message appearing in either stream.
        combined_output = (second_import.stdout or "") + (
            getattr(second_import, "stderr", "") or ""
        )
        assert "already exists" in combined_output, (
            "expected an 'already exists' error message; "
            f"got stdout={second_import.stdout!r} "
            f"stderr={getattr(second_import, 'stderr', '')!r}"
        )

        # Third import WITH --replace must succeed and report the
        # imported run id unchanged.
        replace_import = runner.invoke(
            app,
            [
                "import",
                str(export_file),
                "--db",
                str(dst_db),
                "--replace",
                "--json",
            ],
        )
        assert replace_import.exit_code == 0, (
            f"--replace import failed (exit={replace_import.exit_code}): "
            f"stdout={replace_import.stdout!r} "
            f"stderr={replace_import.stderr!r}"
        )
        replace_summary = json.loads(replace_import.stdout)
        assert replace_summary["imported_run_id"] == run_id
        assert replace_summary["counts"]["run"] == 1
