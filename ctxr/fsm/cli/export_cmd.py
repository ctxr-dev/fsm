"""``ctxr-fsm export`` — dump a single run as a self-contained JSON file.

This command bundles everything the substrate knows about one run into
a versioned JSON document on disk. The output is portable, diff-friendly,
and re-importable via the sibling ``ctxr-fsm import`` command.

Schema (``schema_version = 1``)
-------------------------------

The top-level shape is::

    {
        "schema_version": 1,
        "exported_at": "<ISO-8601 UTC timestamp>",
        "source": {
            "db_path": "<absolute path of source DB>",
            "tool": "ctxr-fsm",
            "tool_version": "<package version>"
        },
        "run":               { ...Run row... },
        "state_tree":        { ...nested StateNode tree, or null... },
        "events":            [ ...Event rows for this run, seq-ordered... ],
        "worker_artifacts":  [ ...WorkerArtifact rows, oldest first... ],
        "aggregates":        [ ...Aggregate rows, oldest first... ],
        "commit_signatures": [ ...CommitSignatureRecord rows, oldest first... ],
        "journal":           { ...newest unfinalised JournalTxn, or null... }
    }

Why versioned
-------------

The shape is stable today but the substrate is still in early
workstreams; bumping ``schema_version`` is the contract we lean on
when the journal carries new keys (W12), commit signatures gain extra
metadata, or the state-tree grows nested loop frames. Consumers MUST
read ``schema_version`` first and fail fast on a value they do not
recognise; the importer in this CLI does exactly that.

Why we go through the lifecycle repos for everything we can
-----------------------------------------------------------

The repos already do the canonical row → value-object projection
(``State`` / ``Transition`` / ``Aggregate`` etc.), so we get
deterministic JSON serialisation via Pydantic's ``model_dump(mode='json')``
without reinventing the encoder. The handful of cross-run queries the
repos do not yet expose (``worker_artifacts by run``, ``aggregates by
run``) are issued as direct SELECTs against the SQLModel tables — kept
narrow and read-only so the abstraction leak is contained to this file.
"""

from __future__ import annotations

import importlib.metadata
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer
from sqlalchemy import select

from ctxr.fsm.cli._common import (
    DB_OPTION,
    JSON_OPTION,
    die,
    json_or_pretty,
    open_project_for_cli,
    resolve_db_path,
)
from ctxr.fsm.sqlite import Project
from ctxr.fsm.sqlite.models_core import AggregateTable, WorkerArtifactTable
from ctxr.fsm.sqlite.models_enforcement import CommitSignatureTable
from ctxr.fsm.sqlite.repos_enforcement import _commit_signature_from_row
from ctxr.fsm.sqlite.repos_states import (
    AggregatesRepo,
    WorkerArtifactsRepo,
)

__all__ = ["EXPORT_SCHEMA_VERSION", "build_export_payload", "export"]


# The current export-document version. Bump (and update the importer's
# ``_SUPPORTED_SCHEMA_VERSIONS`` set) when the shape changes in a way
# that older importers cannot understand. Additive, backwards-compatible
# changes do *not* bump this — they are absorbed silently by Pydantic's
# ``extra='ignore'`` default at the consumer side.
EXPORT_SCHEMA_VERSION: int = 1


# Module-level singletons for the export command's positional arguments.
# Pulled out so the function signature stays short and so ruff's ``B008``
# (no function calls in argument defaults) is satisfied — the typer
# call happens once at import time rather than once per function call.
_RUN_ID_ARG: Any = typer.Argument(
    ...,
    help="Run id to export (full UUID; prefix support is TODO).",
)
_OUTPUT_PATH_ARG: Any = typer.Argument(
    ...,
    help=(
        "Destination JSON file. Use '-' to write the export to "
        "stdout instead of a file (handy for piping to `jq` / "
        "`fsm import` directly)."
    ),
)
_OVERWRITE_OPTION: Any = typer.Option(
    False,
    "--overwrite",
    help=(
        "Overwrite ``output_path`` if it already exists. Without "
        "this flag we refuse to clobber an existing file so a typo "
        "cannot lose a previous export."
    ),
)


def _now_iso_z() -> str:
    """Return now() as canonical ``YYYY-MM-DDTHH:MM:SS.sssZ``.

    Matches the timestamp shape used by the journal/event tables so a
    consumer that sorts the export's ``exported_at`` next to event
    ``created_at`` values gets a coherent timeline.
    """
    return (
        datetime.now(tz=UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


def _tool_version() -> str:
    """Best-effort lookup of the ``ctxr-fsm`` package version.

    Falls back to the literal ``"unknown"`` rather than raising — the
    export must succeed even when the package is being run from a
    source checkout that has no installed dist-info.
    """
    try:
        return importlib.metadata.version("ctxr-fsm")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _worker_artifacts_for_run(project: Project, run_id: str) -> list[dict[str, Any]]:
    """Return every worker-artifact row for ``run_id`` as JSON-shaped dicts.

    ``WorkerArtifactsRepo`` exposes ``by_state`` but no ``by_run`` —
    we issue a single SELECT keyed on ``run_id`` and route the rows
    through the repo's private ``_to_value`` helper so the
    serialisation path matches every other consumer of the table.

    Ordering: ``state_id``, then ``iteration_n`` (NULLs first so the
    no-loop case sorts ahead of loop iterations), then ``created_at``
    as a stable tiebreaker. This matches the ordering ``by_state``
    exposes per-state, just globalised across the run.
    """
    repo = WorkerArtifactsRepo()
    stmt = (
        select(WorkerArtifactTable)
        .where(WorkerArtifactTable.run_id == run_id)
        .order_by(
            WorkerArtifactTable.state_id.asc(),
            WorkerArtifactTable.iteration_n.asc().nulls_first(),
            WorkerArtifactTable.created_at.asc(),
        )
    )
    with project.session_factory() as session:
        rows = session.execute(stmt).scalars().all()
        return [repo._to_value(row).model_dump(mode="json") for row in rows]


def _aggregates_for_run(project: Project, run_id: str) -> list[dict[str, Any]]:
    """Return every aggregate row for ``run_id`` as JSON-shaped dicts.

    ``AggregatesRepo.get`` returns *latest* per field; for export we
    want every recorded aggregate so the round-trip is lossless.
    Ordering by ``created_at`` keeps the timeline reproducible.
    """
    repo = AggregatesRepo()
    stmt = (
        select(AggregateTable)
        .where(AggregateTable.run_id == run_id)
        .order_by(AggregateTable.created_at.asc(), AggregateTable.id.asc())
    )
    with project.session_factory() as session:
        rows = session.execute(stmt).scalars().all()
        return [repo._to_value(row).model_dump(mode="json") for row in rows]


def _commit_signatures_for_run(
    project: Project, run_id: str
) -> list[dict[str, Any]]:
    """Return every commit-signature row for ``run_id`` as JSON-shaped dicts.

    ``CommitSignaturesRepo.last_for_run`` only returns the freshest row;
    a full export needs the entire chain so we hit the table directly.
    Ordering by ``created_at`` keeps the sequence of commits in the
    order the engine emitted them.
    """
    stmt = (
        select(CommitSignatureTable)
        .where(CommitSignatureTable.run_id == run_id)
        .order_by(
            CommitSignatureTable.created_at.asc(),
            CommitSignatureTable.id.asc(),
        )
    )
    with project.session_factory() as session:
        rows = session.execute(stmt).scalars().all()
        return [
            _commit_signature_from_row(row).model_dump(mode="json") for row in rows
        ]


def build_export_payload(
    project: Project,
    run_id: str,
    *,
    db_path: Path,
) -> dict[str, Any]:
    """Materialise the full export document for ``run_id``.

    Pulled out from :func:`export` so callers (tests, scripts, the
    eventual W5 ``/runs/{id}/export`` HTTP endpoint) can reuse the
    builder without going through the file-write half of the command.

    Raises ``LookupError`` when ``run_id`` is unknown — the CLI layer
    translates that into a typed ``typer.Exit`` with a friendly message.
    """
    run = project.get_run(run_id)
    if run is None:
        raise LookupError(f"no run with id {run_id!r}")

    # State tree, events, journal — these all have repo accessors that
    # take a session. We open one session for the bundle so the reads
    # see a consistent snapshot (SQLite REPEATABLE-READ on a single
    # connection within an implicit transaction).
    with project.session_factory() as session:
        tree = project.runs.state_tree(session, run_id)
        # ``events`` is an iterator — materialise eagerly so the JSON
        # encoder sees a concrete list and the session can close.
        event_rows = list(project.runs.events(session, run_id))
        journal = project.journal.inspect(session, run_id=run_id)

    # The three "by_run" projections that the repos don't expose go
    # through the helpers above; each opens its own session because
    # they are simple read SELECTs and we'd rather keep the snapshot
    # boundaries narrow than force a long-lived session here.
    worker_artifacts = _worker_artifacts_for_run(project, run_id)
    aggregates = _aggregates_for_run(project, run_id)
    commit_signatures = _commit_signatures_for_run(project, run_id)

    return {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "exported_at": _now_iso_z(),
        "source": {
            "db_path": str(db_path),
            "tool": "ctxr-fsm",
            "tool_version": _tool_version(),
        },
        "run": run.model_dump(mode="json"),
        "state_tree": tree.model_dump(mode="json") if tree is not None else None,
        "events": [event.model_dump(mode="json") for event in event_rows],
        "worker_artifacts": worker_artifacts,
        "aggregates": aggregates,
        "commit_signatures": commit_signatures,
        "journal": journal.model_dump(mode="json") if journal is not None else None,
    }


def export(
    run_id: str = _RUN_ID_ARG,
    output_path: Path = _OUTPUT_PATH_ARG,
    db: Path | None = DB_OPTION,
    json_mode: bool = JSON_OPTION,
    overwrite: bool = _OVERWRITE_OPTION,
) -> None:
    """Dump ``run_id`` into ``output_path`` as a versioned JSON document.

    The output is the result of :func:`build_export_payload` serialised
    with ``sort_keys=True`` + 2-space indent so two exports of the same
    run produce byte-identical files — important for diff-based audit
    workflows that compare exports across CI runs.

    When ``output_path == '-'`` the payload is written to stdout
    instead. The ``--json`` flag controls the *summary* output (printed
    to stdout when writing to a file, suppressed when writing to stdout
    so the JSON document is the only thing on the wire).
    """
    db_path = resolve_db_path(db)
    write_to_stdout = str(output_path) == "-"

    if not write_to_stdout and output_path.exists() and not overwrite:
        die(
            f"refusing to overwrite existing file {output_path!s} "
            "(pass --overwrite to force)"
        )

    with open_project_for_cli(db_path) as project:
        try:
            payload = build_export_payload(project, run_id, db_path=db_path)
        except LookupError as exc:
            # Translate the typed substrate error into a CLI-friendly
            # message + exit code. Going through ``die`` keeps the
            # error formatting consistent with every other CLI command.
            die(str(exc))

    # Canonical JSON: sort_keys + indent=2 makes the file diff-friendly,
    # which is the whole point of having an export format in the first
    # place. ``default=str`` is the belt-and-braces fallback for any
    # value the encoder doesn't know how to handle natively (e.g. a
    # Pydantic ``datetime`` that snuck through ``model_dump(mode='json')``).
    serialised = json.dumps(payload, sort_keys=True, indent=2, default=str)

    if write_to_stdout:
        # Writing the document itself to stdout means the summary would
        # corrupt the JSON; we honour ``--json`` by simply emitting the
        # bare document and skipping the summary entirely.
        sys.stdout.write(serialised)
        sys.stdout.write("\n")
        sys.stdout.flush()
        return

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(serialised + "\n", encoding="utf-8")

    summary: dict[str, Any] = {
        "run_id": run_id,
        "output_path": str(output_path),
        "schema_version": EXPORT_SCHEMA_VERSION,
        "counts": {
            "events": len(payload["events"]),
            "worker_artifacts": len(payload["worker_artifacts"]),
            "aggregates": len(payload["aggregates"]),
            "commit_signatures": len(payload["commit_signatures"]),
            "state_tree": 0 if payload["state_tree"] is None else 1,
            "journal": 0 if payload["journal"] is None else 1,
        },
        "bytes_written": len(serialised) + 1,
    }
    json_or_pretty(summary, json_mode)
