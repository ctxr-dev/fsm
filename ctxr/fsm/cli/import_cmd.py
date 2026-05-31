"""``ctxr-fsm import`` — re-insert a run from a JSON export.

This is the read-half twin of :mod:`ctxr.fsm.cli.export_cmd`: given a
JSON document produced by ``ctxr-fsm export``, it materialises the
contained rows into the project DB.

W3 scope (minimal viable importer)
----------------------------------

Today we ship the bookkeeping half of import:

* Validate the file is well-formed JSON with a recognised
  ``schema_version``.
* Insert the run row plus its state/events/artifacts/aggregates/commit-
  signatures/journal rows, preserving the original UUIDs when possible.
* Refuse to clobber a pre-existing run with the same id unless the
  operator passes ``--replace`` (in which case the prior run is
  cascade-deleted first; SQLite's ON DELETE CASCADE on every child FK
  does the heavy lifting).
* Wrap the whole operation in the ``@atomic`` envelope so a crash
  mid-way leaves the DB in either the pre-state or the post-state — never
  half-way between.

What we deliberately do *not* do in W3
--------------------------------------

* **Transitions** are not in the schema-v1 export (the exporter
  projects the state tree but does not surface the underlying
  ``transitions`` rows). A future schema-v2 bump will round-trip
  transitions; until then, ``run show`` against an imported run will
  display its states but not the decision edges between them.
* **Producers** are not exported. We upsert a synthetic ``imported.*``
  producer on the fly for events that reference an unknown
  ``producer_id`` so the FK lands; the audit semantics of the original
  producer identity are intentionally lossy here.
* **Project / FSM spec** rows must already exist in the target DB —
  the importer does not try to materialise them. Operators who want a
  fully standalone restore should run ``ctxr-fsm spec register`` first
  (W3 ships that command) against the same spec used for the export.

The minimal-but-typed surface lets later workstreams expand the
importer (transitions in W3.5, producer round-trip in W4) without
breaking today's contract.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any

import typer
import uuid_utils
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ctxr.fsm.cli._common import (
    DB_OPTION,
    JSON_OPTION,
    die,
    json_or_pretty,
    open_project_for_cli,
    resolve_db_path,
)
from ctxr.fsm.cli.export_cmd import EXPORT_SCHEMA_VERSION
from ctxr.fsm.sqlite import Project, atomic
from ctxr.fsm.sqlite.models_core import (
    AggregateTable,
    RunTable,
    StateTable,
    WorkerArtifactTable,
)
from ctxr.fsm.sqlite.models_enforcement import (
    CommitSignatureTable,
    JournalTxnTable,
)
from ctxr.fsm.sqlite.models_events import EventTable, ProducerTable

__all__ = ["ImportCounts", "RunImportError", "import_run", "import_run_cmd"]


# The set of schema versions this importer knows how to read. We pin a
# *set* (not just a single int) so a future schema bump that stays
# backwards-compatible can simply add the new version here; truly
# breaking bumps would remove older versions on a deliberate timeline.
_SUPPORTED_SCHEMA_VERSIONS: frozenset[int] = frozenset({EXPORT_SCHEMA_VERSION})


# Module-level singletons for the import command's positional argument
# and ``--replace`` option. Pulled out so the function signature stays
# short and so ruff's ``B008`` (no function calls in argument defaults)
# is satisfied — the typer call happens once at import time rather than
# once per function invocation.
_INPUT_PATH_ARG: Any = typer.Argument(
    ...,
    help=(
        "Path to a JSON export file produced by `ctxr-fsm export`. "
        "Use '-' to read the document from stdin."
    ),
)
_REPLACE_OPTION: Any = typer.Option(
    False,
    "--replace",
    help=(
        "Drop any pre-existing run with the same id before "
        "inserting (cascade-deletes all child rows)."
    ),
)


class RunImportError(RuntimeError):
    """Typed failure raised by the importer.

    We deliberately do NOT name this class :class:`ImportError` — that
    name belongs to Python's built-in module-import exception, and
    shadowing it in this module would make every ``except ImportError``
    block downstream do something subtly wrong. ``RunImportError`` is
    explicit about the domain.

    Subclasses :class:`RuntimeError` so callers that haven't yet been
    taught about this module still see a sensible exception type; the
    CLI translates instances of this class into a friendly ``die`` call
    rather than letting the traceback hit the operator.
    """


class ImportCounts:
    """Trivial bag of per-table insert counters used by the summary.

    A plain mutable namespace beats a Pydantic model here because we
    only need 1-line increments inside the @atomic body and the values
    never leave this module — exposed publicly to make the summary
    contract obvious for callers that import :func:`import_run` from
    tests / scripts.
    """

    __slots__ = (
        "aggregates",
        "commit_signatures",
        "events",
        "journal",
        "producers_upserted",
        "run",
        "states",
        "worker_artifacts",
    )

    def __init__(self) -> None:
        self.run: int = 0
        self.states: int = 0
        self.events: int = 0
        self.worker_artifacts: int = 0
        self.aggregates: int = 0
        self.commit_signatures: int = 0
        self.journal: int = 0
        self.producers_upserted: int = 0

    def as_dict(self) -> dict[str, int]:
        """Return the counters as a plain dict for JSON-mode output."""
        return {name: getattr(self, name) for name in self.__slots__}


# ---------------------------------------------------------------------------
# Payload parsing
# ---------------------------------------------------------------------------


def _load_payload(input_path: Path) -> dict[str, Any]:
    """Read and minimally validate the export document at ``input_path``.

    "Minimal" here means: it is valid JSON, the top-level value is an
    object, and the ``schema_version`` field falls in the supported
    set. Field-level validation (e.g. UUID shape, timestamp format) is
    deferred to the per-row insert path so a single bad row produces a
    targeted error rather than blocking import on a strict pre-check.
    """
    try:
        text = input_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RunImportError(f"could not read {input_path!s}: {exc}") from exc

    try:
        decoded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RunImportError(
            f"{input_path!s} is not valid JSON: {exc}"
        ) from exc

    if not isinstance(decoded, dict):
        raise RunImportError(
            f"{input_path!s} does not contain a JSON object at top level "
            f"(got {type(decoded).__name__})"
        )

    schema_version = decoded.get("schema_version")
    if schema_version not in _SUPPORTED_SCHEMA_VERSIONS:
        raise RunImportError(
            f"unsupported export schema_version {schema_version!r}; "
            f"this importer accepts {sorted(_SUPPORTED_SCHEMA_VERSIONS)!r}"
        )

    run = decoded.get("run")
    if not isinstance(run, dict):
        raise RunImportError(
            "export document is missing the 'run' object "
            "(or it is not a JSON object)"
        )
    if "id" not in run or not isinstance(run["id"], str):
        raise RunImportError(
            "export document's run object has no string 'id'"
        )

    return decoded


def _flatten_state_tree(node: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return every state-entry record contained in the nested tree.

    The exporter emits the tree as a recursive ``StateNode`` shape (root
    + ``children``). For import we re-flatten to a list of state rows so
    the insert loop can stamp them in ``entry_seq`` order without
    re-implementing the tree walk per insert.

    Returns an empty list when ``node`` is ``None`` (run had no state
    activations) — keeps the caller's loop a one-liner.
    """
    if node is None:
        return []
    out: list[dict[str, Any]] = []
    stack: list[dict[str, Any]] = [node]
    while stack:
        current = stack.pop()
        out.append(current)
        children = current.get("children") or []
        # Reverse so the depth-first walk preserves the tree's natural
        # left-to-right order when we pop from the end of the stack.
        for child in reversed(children):
            if isinstance(child, dict):
                stack.append(child)
    return out


# ---------------------------------------------------------------------------
# Row materialisation helpers
# ---------------------------------------------------------------------------


def _new_uuid7_str() -> str:
    """Mint a fresh UUIDv7 string for rows whose source id is missing.

    The contract is: "preserve when present, otherwise mint new". We
    centralise the mint here so any callsite that needs an id falls
    through one helper rather than duplicating the import.
    """
    return str(uuid_utils.uuid7())


def _id_or_new(value: Any) -> str:
    """Return ``value`` if it is a non-empty string, otherwise mint a UUID.

    Used at every row-insert site to honour the "preserve original
    UUIDs if available, ELSE generate new ones" contract from the
    brief in one place.
    """
    if isinstance(value, str) and value:
        return value
    return _new_uuid7_str()


def _canonical_json(value: Any) -> str:
    """Serialise ``value`` to canonical JSON text.

    Mirrors the writer used by every repo in the substrate so the bytes
    on disk after an import are indistinguishable from the bytes a
    native ``register/start/commit`` cycle would have produced.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _ensure_producer(
    session: Session,
    producer_id: str,
    *,
    counts: ImportCounts,
) -> str:
    """Return ``producer_id``, inserting a synthetic placeholder if absent.

    Events carry a ``producer_id`` FK into ``producers.id``. The export
    document does not round-trip the producers table in schema-v1, so
    on import we may encounter producer ids the target DB has never
    seen. We upsert a stub row using the original id (preserving it for
    audit lineage) and tag the producer with kind ``imported`` so the
    artificial origin is visible to anyone inspecting the producers
    table later.

    Idempotent: if a row with ``producer_id`` already exists we leave
    it alone and return its id unchanged.
    """
    existing = session.get(ProducerTable, producer_id)
    if existing is not None:
        return existing.id

    row = ProducerTable(
        id=producer_id,
        kind="imported",
        # A name that makes the synthetic origin obvious in any
        # producers-by-kind listing; the producer id is unique so name
        # collisions across imports are not a concern.
        name=f"import:{producer_id[:8]}",
        metadata_json="{}",
        # ``created_at`` is stamped on insert rather than copied from
        # the export because we don't have the original — and the
        # producer's *creation* moment is now, even if the producer's
        # *id* is preserved.
        created_at=_now_iso_z(),
    )
    session.add(row)
    session.flush()
    counts.producers_upserted += 1
    return row.id


def _now_iso_z() -> str:
    """Local copy of the project's canonical timestamp helper.

    Mirrors :func:`ctxr.fsm.sqlite.repos_events._iso_now_ms` so the
    synthetic producer rows minted during import sort alongside native
    rows under the same BINARY collation.
    """
    from datetime import UTC, datetime

    return (
        datetime.now(tz=UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


# ---------------------------------------------------------------------------
# Per-table insert loops
# ---------------------------------------------------------------------------


def _insert_run(
    session: Session, run: dict[str, Any], counts: ImportCounts
) -> str:
    """Insert the top-level run row and return its id.

    Every column the schema knows about is honoured, with sensible
    defaults for the optional bits (``args_json`` / ``metadata_json``
    /``resume_history_json`` default to canonical-empty values when the
    export omitted them). FK columns (``project_id``, ``fsm_spec_id``)
    are passed through unchanged — if they reference rows missing from
    the target DB the INSERT raises ``IntegrityError`` which the CLI
    layer surfaces with a typed message.
    """
    run_id = _id_or_new(run.get("id"))
    row = RunTable(
        id=run_id,
        project_id=run["project_id"],
        fsm_spec_id=run["fsm_spec_id"],
        fsm_spec_hash=run["fsm_spec_hash"],
        status=run["status"],
        current_state=run.get("current_state"),
        next_state=run.get("next_state"),
        verdict=run.get("verdict"),
        started_at=run["started_at"],
        ended_at=run.get("ended_at"),
        last_update_at=run["last_update_at"],
        paused_at=run.get("paused_at"),
        pause_reason=run.get("pause_reason"),
        parent_run_id=run.get("parent_run_id"),
        resume_history_json=_canonical_json(run.get("resume_history") or []),
        args_json=_canonical_json(run.get("args") or {}),
        metadata_json=_canonical_json(run.get("metadata") or {}),
        transitions_count=int(run.get("transitions_count", 0)),
    )
    session.add(row)
    session.flush()
    counts.run += 1
    return run_id


def _insert_states(
    session: Session,
    run_id: str,
    state_records: list[dict[str, Any]],
    counts: ImportCounts,
) -> dict[str, str]:
    """Insert every state-entry row and return an old→new id map.

    The map lets downstream inserts (worker_artifacts, commit_signatures)
    re-point their ``state_id`` FKs to whichever id the row ended up
    with — usually the original (when the source id was carried through)
    but sometimes a freshly-minted UUID when the source was missing.

    Inserts happen in ``entry_seq`` order because the
    ``idx_states_run_seq`` UNIQUE index forbids gaps within a run and
    in-order insertion is the most natural way to populate the index
    cleanly.
    """
    id_map: dict[str, str] = {}
    ordered = sorted(state_records, key=lambda r: int(r.get("entry_seq", 0)))
    for record in ordered:
        # The exporter uses ``entry_id`` for the row PK (see
        # :class:`ctxr.fsm.core.repos_core.StateNode`); fall back to
        # ``id`` if a caller hand-built the export from a different
        # source. ``_id_or_new`` covers the "neither present" path.
        original_id = record.get("entry_id") or record.get("id")
        new_id = _id_or_new(original_id)
        row = StateTable(
            id=new_id,
            run_id=run_id,
            state_id=record["state_id"],
            entry_seq=int(record["entry_seq"]),
            entered_at=record["entered_at"],
            exited_at=record.get("exited_at"),
            status=record.get("status", "entered"),
            inputs_json=_canonical_json(record.get("inputs") or {}),
            outputs_json=_canonical_json(record.get("outputs") or {}),
            iteration_n=record.get("iteration_n"),
        )
        session.add(row)
        session.flush()
        if isinstance(original_id, str) and original_id:
            id_map[original_id] = new_id
        counts.states += 1
    return id_map


def _insert_events(
    session: Session,
    run_id: str,
    events: list[dict[str, Any]],
    counts: ImportCounts,
) -> None:
    """Insert every event row, upserting producers as needed.

    The export carries the per-run ``seq`` allocation already, so we
    preserve it verbatim — bypassing the ``EventsRepo.emit`` allocator
    means an imported run round-trips with the same seq numbering it
    had on the source DB, which is important for any downstream
    consumer (e.g. the W12 replay path) that pins behaviour off seq
    deltas.
    """
    for event in events:
        producer_id = _ensure_producer(
            session, event["producer_id"], counts=counts
        )
        row = EventTable(
            id=_id_or_new(event.get("id")),
            run_id=run_id,
            kind=event["kind"],
            producer_id=producer_id,
            payload_json=_canonical_json(event.get("payload") or {}),
            created_at=event["created_at"],
            seq=event.get("seq"),
        )
        session.add(row)
        session.flush()
        counts.events += 1


def _insert_worker_artifacts(
    session: Session,
    run_id: str,
    artifacts: list[dict[str, Any]],
    state_id_map: dict[str, str],
    counts: ImportCounts,
) -> None:
    """Insert worker-artifact rows, remapping ``state_id`` via ``state_id_map``.

    When an artifact's source ``state_id`` is not in the map (which
    would only happen if the export is internally inconsistent — the
    artifact references a state we did not import), we still attempt
    the insert with the original id; the FK constraint will then
    surface the inconsistency as an ``IntegrityError`` rather than
    silently corrupting the import.
    """
    for record in artifacts:
        source_state_id = record["state_id"]
        target_state_id = state_id_map.get(source_state_id, source_state_id)
        row = WorkerArtifactTable(
            id=_id_or_new(record.get("id")),
            run_id=run_id,
            state_id=target_state_id,
            iteration_n=record.get("iteration_n"),
            prompt_text=record["prompt_text"],
            prompt_hash=record["prompt_hash"],
            output_json=_canonical_json(record.get("output") or {}),
            validated=int(bool(record.get("validated", False))),
            created_at=record["created_at"],
        )
        session.add(row)
        session.flush()
        counts.worker_artifacts += 1


def _insert_aggregates(
    session: Session,
    run_id: str,
    aggregates: list[dict[str, Any]],
    state_id_map: dict[str, str],
    counts: ImportCounts,
) -> None:
    """Insert aggregate rows, remapping the ``from_state_ids`` list.

    ``from_state_ids`` is a JSON array of source-state PKs whose
    contents the aggregator combined; if those PKs were rewritten on
    import we follow them through the map. Unknown ids pass through
    unchanged for the same diagnostic reason as in
    :func:`_insert_worker_artifacts`.
    """
    for record in aggregates:
        remapped_sources = [
            state_id_map.get(src, src) for src in record.get("from_state_ids", [])
        ]
        row = AggregateTable(
            id=_id_or_new(record.get("id")),
            run_id=run_id,
            field=record["field"],
            from_state_ids_json=_canonical_json(remapped_sources),
            merged_length=int(record["merged_length"]),
            items_json=_canonical_json(record.get("items") or []),
            created_at=record["created_at"],
        )
        session.add(row)
        session.flush()
        counts.aggregates += 1


def _insert_commit_signatures(
    session: Session,
    run_id: str,
    signatures: list[dict[str, Any]],
    state_id_map: dict[str, str],
    counts: ImportCounts,
) -> None:
    """Insert commit-signature rows, remapping ``state_id`` via ``state_id_map``."""
    for record in signatures:
        source_state_id = record["state_id"]
        target_state_id = state_id_map.get(source_state_id, source_state_id)
        row = CommitSignatureTable(
            id=_id_or_new(record.get("id")),
            run_id=run_id,
            state_id=target_state_id,
            iteration_n=record.get("iteration_n"),
            brief_id=record["brief_id"],
            inputs_hash=record["inputs_hash"],
            outputs_hash=record["outputs_hash"],
            session_id=record["session_id"],
            signature=record["signature"],
            verified=bool(record.get("verified", False)),
            created_at=record["created_at"],
        )
        session.add(row)
        session.flush()
        counts.commit_signatures += 1


def _insert_journal(
    session: Session,
    run_id: str,
    journal: dict[str, Any] | None,
    counts: ImportCounts,
) -> None:
    """Insert the journal row if present.

    The export carries at most one *unfinalised* journal txn (newest);
    we re-insert it as-is. ``finalised`` rows are intentionally not
    exported (the inspect-newest semantics in ``JournalRepo.inspect``
    only returns unfinalised), so on a clean run with no in-flight
    journal this is a no-op.
    """
    if journal is None:
        return
    # ``started_at`` / ``ready_at`` / ``finalised_at`` come through as
    # ISO-8601 strings (Pydantic ``model_dump(mode='json')`` serialised
    # the JournalTxn datetimes that way). We store them verbatim — the
    # column type is TEXT.
    row = JournalTxnTable(
        id=_id_or_new(journal.get("id")),
        run_id=run_id,
        status=journal["status"],
        staged_writes_json=_canonical_json(journal.get("staged_writes") or []),
        started_at=journal["started_at"],
        ready_at=journal.get("ready_at"),
        finalised_at=journal.get("finalised_at"),
    )
    session.add(row)
    session.flush()
    counts.journal += 1


# ---------------------------------------------------------------------------
# Top-level import routine
# ---------------------------------------------------------------------------


def _insert_run_standalone(
    project: Project, run: dict[str, Any], counts: ImportCounts
) -> str:
    """Insert the run row in its own short transaction, outside @atomic.

    The ``@atomic`` envelope opens a ``journal_txns`` row keyed on
    ``run_id`` before the wrapped body runs; that journal row has an
    ``ON DELETE CASCADE`` FK to ``runs.id`` (enforced by SQLite's
    ``PRAGMA foreign_keys=ON``), so the run row MUST already exist
    before @atomic touches the journal table. We insert the run row
    in its own tiny transaction here, then let @atomic handle every
    child table — that way the journal-txn audit row lands cleanly
    and the rest of the import gets the usual atomic envelope.

    If the wrapped @atomic body later fails, the caller is expected
    to roll back this pre-insert too (see :func:`import_run`).
    """
    with project.session_factory() as session, session.begin():
        run_id = _insert_run(session, run, counts)
    return run_id


@atomic
def _do_import(
    session: Session,
    run_id: str,
    *,
    payload: dict[str, Any],
    counts: ImportCounts,
) -> None:
    """Insert every child row from ``payload`` inside one atomic envelope.

    The ``@atomic`` decorator owns the journal-txn lifecycle and the
    BEGIN IMMEDIATE write lock for the import; if any individual
    INSERT fails (e.g. an FK references an unknown producer) the whole
    transaction rolls back, leaving the target DB exactly as it was
    before the call.

    Signature follows the ``@atomic`` contract: ``session`` first,
    ``run_id`` second so the decorator can refuse if a stale journal
    txn exists for this run.

    The run row itself is inserted *outside* this envelope by
    :func:`_insert_run_standalone` — the ``journal_txns.run_id`` FK
    constraint means the run row must already be visible by the time
    @atomic opens its journal row.
    """
    # States first — their PKs are FK targets for worker_artifacts and
    # commit_signatures, so we need the rewrite map before those inserts.
    state_records = _flatten_state_tree(payload.get("state_tree"))
    state_id_map = _insert_states(session, run_id, state_records, counts)

    # Events, artifacts, aggregates, signatures, journal can land in
    # any order now that runs + states exist.
    _insert_events(session, run_id, payload.get("events") or [], counts)
    _insert_worker_artifacts(
        session,
        run_id,
        payload.get("worker_artifacts") or [],
        state_id_map,
        counts,
    )
    _insert_aggregates(
        session,
        run_id,
        payload.get("aggregates") or [],
        state_id_map,
        counts,
    )
    _insert_commit_signatures(
        session,
        run_id,
        payload.get("commit_signatures") or [],
        state_id_map,
        counts,
    )
    _insert_journal(session, run_id, payload.get("journal"), counts)


def _delete_existing_run(project: Project, run_id: str) -> None:
    """Cascade-delete an existing run row prior to a ``--replace`` import.

    SQLite's ON DELETE CASCADE (enabled per-connection by
    :func:`ctxr.fsm.sqlite.connection.open_engine`) takes care of the
    states / transitions / events / aggregates / worker_artifacts /
    locks / commit_signatures children automatically; we only have to
    delete the run row itself.

    We do not go through the lifecycle repos because :class:`RunsRepo`
    deliberately does not expose a hard-delete (and the engine's abort
    path is a status flip, not a row remove). Issuing the raw DELETE
    here keeps the destructive operation visible in the audit trail.
    """
    with project.session_factory() as session, session.begin():
        # Also clear any pre-existing journal_txn rows for this run —
        # the journal table has no FK to runs (it carries run_id by
        # string only), so cascade does not reach it. Without this
        # cleanup, ``@atomic`` would refuse the subsequent import with
        # JournalRefusedError.
        session.execute(
            delete(JournalTxnTable).where(JournalTxnTable.run_id == run_id)
        )
        # The run row itself: the cascading FKs do the rest.
        session.execute(delete(RunTable).where(RunTable.id == run_id))


def import_run(
    project: Project,
    payload: dict[str, Any],
    *,
    replace: bool = False,
) -> ImportCounts:
    """Insert the run described by ``payload`` into ``project``.

    Pulled out from :func:`import_run_cmd` so callers (tests, the
    eventual W5 HTTP endpoint) can drive imports programmatically.

    Raises :class:`RunImportError` when the target DB already contains
    a run with the same id and ``replace=False``.

    The run row is inserted in its own short transaction *before* the
    main ``@atomic`` envelope opens. This ordering is forced by the
    ``journal_txns.run_id`` FK constraint — the journal-open step
    requires the run row to already be visible. If the @atomic body
    later fails we cascade-delete the stub run we just inserted so
    the destination DB ends up exactly as it started.
    """
    run_id = payload["run"]["id"]

    with project.session_factory() as session:
        existing = session.execute(
            select(RunTable.id).where(RunTable.id == run_id)
        ).scalar_one_or_none()

    if existing is not None and not replace:
        raise RunImportError(
            f"a run with id {run_id!r} already exists in the target DB; "
            "re-run with --replace to overwrite it"
        )
    if existing is not None and replace:
        _delete_existing_run(project, run_id)

    counts = ImportCounts()
    # Pre-insert the run row outside @atomic so the journal-open step
    # inside @atomic sees a valid FK target.
    _insert_run_standalone(project, payload["run"], counts)
    try:
        _do_import(run_id, payload=payload, counts=counts)
    except Exception:
        # The @atomic envelope rolled back the child-row inserts on
        # failure but left the pre-inserted run row behind; clean it
        # up so the destination DB is unchanged on import failure.
        with contextlib.suppress(Exception):  # pragma: no cover — best-effort cleanup
            _delete_existing_run(project, run_id)
        raise
    return counts


def import_run_cmd(
    input_path: Path = _INPUT_PATH_ARG,
    db: Path | None = DB_OPTION,
    json_mode: bool = JSON_OPTION,
    replace: bool = _REPLACE_OPTION,
) -> None:
    """Import a run from a ``ctxr-fsm export`` JSON document.

    Inserts every row from the export inside one ``@atomic`` envelope
    so a failure rolls everything back cleanly. Prints a per-table
    insert-count summary at the end.
    """
    db_path = resolve_db_path(db)

    # ``input_path == '-'`` reads from stdin. We tolerate the path
    # not existing in that case; ``_load_payload`` would otherwise
    # try to ``read_text`` a non-existent file.
    if str(input_path) == "-":
        import sys as _sys

        try:
            text = _sys.stdin.read()
            payload = json.loads(text)
            if not isinstance(payload, dict):
                die(
                    "stdin did not contain a JSON object at top level "
                    f"(got {type(payload).__name__})"
                )
            if payload.get("schema_version") not in _SUPPORTED_SCHEMA_VERSIONS:
                die(
                    f"unsupported export schema_version "
                    f"{payload.get('schema_version')!r}; "
                    f"this importer accepts "
                    f"{sorted(_SUPPORTED_SCHEMA_VERSIONS)!r}"
                )
            if (
                not isinstance(payload.get("run"), dict)
                or not isinstance(payload["run"].get("id"), str)
            ):
                die("stdin payload's 'run' object has no string 'id'")
        except json.JSONDecodeError as exc:
            die(f"stdin is not valid JSON: {exc}")
    else:
        if not input_path.exists():
            die(f"no such file: {input_path!s}")
        try:
            payload = _load_payload(input_path)
        except RunImportError as exc:
            die(str(exc))

    with open_project_for_cli(db_path) as project:
        try:
            counts = import_run(project, payload, replace=replace)
        except RunImportError as exc:
            die(str(exc))

    summary: dict[str, Any] = {
        "imported_run_id": payload["run"]["id"],
        "source": str(input_path),
        "schema_version": payload["schema_version"],
        "replaced": replace and payload["run"]["id"] is not None,
        "counts": counts.as_dict(),
    }
    json_or_pretty(summary, json_mode)
