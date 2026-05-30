"""``ctxr-fsm runs`` / ``ctxr-fsm run`` — run inspection and management.

This module defines two related Typer sub-apps so the command surface
matches operator muscle memory:

* ``ctxr-fsm runs`` — *plural*, for queries that span multiple runs
  (currently just ``ls``).
* ``ctxr-fsm run`` — *singular*, for commands that target a specific
  run (``show``, ``resume``, ``abort``).

Both sub-apps are constructed here and wired into the top-level
:data:`ctxr.fsm.cli.app` by :mod:`ctxr.fsm.cli` at import time. Keeping
the two together in one module is deliberate: the helpers that resolve
a run id from a (possibly abbreviated) prefix, build the pretty rich
table, and emit JSON payloads are shared by every command, and a single
module is the simplest place to put them.

W3 scope vs. later workstreams
------------------------------

The engine-driven resume path (state machine takeover, journal
replay-into-engine, etc.) is W12. This module ships the *bookkeeping*
half of resume — flipping the journal row and emitting the structured
event — and prints an explicit "engine-driven resume comes in a later
workstream" notice when the caller asks for ``--from-state``. That way
operators have a stable CLI contract today, and the runtime integration
in W12 only needs to grow the inner loop, not re-design the command
shape.

Prefix resolution
-----------------

The brief mentions accepting 7-char id prefixes; until the substrate
exposes a prefix-search helper, this module accepts the full UUID only
and surfaces a TODO in the help text. The shape of the helper is
already factored out into :func:`_resolve_run_id` so wiring a prefix
search in later is a one-line change.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer
from rich import print as rich_print
from rich.table import Table

from ctxr.fsm.cli._common import (
    DB_OPTION,
    JSON_OPTION,
    die,
    json_or_pretty,
    open_project_for_cli,
    resolve_db_path,
)
from ctxr.fsm.core.models import EventKind
from ctxr.fsm.sqlite import Project
from ctxr.fsm.sqlite.repos_core import RunSummary, StateNode

__all__ = ["run_app", "runs_app"]


# ---------------------------------------------------------------------------
# Typer sub-apps
# ---------------------------------------------------------------------------

# ``runs`` (plural) groups the cross-run queries. ``no_args_is_help=True``
# means ``ctxr-fsm runs`` (no subcommand) prints the help screen instead
# of doing nothing, which is friendlier when an operator is exploring.
runs_app: typer.Typer = typer.Typer(
    name="runs",
    help="Inspect and manage FSM runs.",
    no_args_is_help=True,
    add_completion=False,
)


# ``run`` (singular) groups the per-run commands. We split it out from
# ``runs`` so the syntax stays English-readable: "list the runs" reads
# as ``runs ls``; "show this run" reads as ``run show <id>``.
run_app: typer.Typer = typer.Typer(
    name="run",
    help="Per-run commands (show / resume / abort).",
    no_args_is_help=True,
    add_completion=False,
)


# The producer identity we attribute CLI-emitted events to. We mirror
# the names used by ``Project.start_run`` for the engine producer
# (``kind='engine'``, ``name='fsm.runtime'``) so downstream consumers
# see one logical producer regardless of whether the emit came from the
# engine itself or the operator-facing CLI. That keeps the audit trail
# clean: ``producer_id`` is stable across the lifetime of the run.
_CLI_PRODUCER_KIND: str = "engine"
_CLI_PRODUCER_NAME: str = "fsm.runtime"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _short_id(value: str | None, width: int = 7) -> str:
    """Return the first ``width`` characters of an id, or ``"-"`` if empty.

    Used by the pretty rich-table renderer so wide UUID columns do not
    swamp the terminal. The same shortening is *not* applied to JSON
    output — machine consumers should always see the full id.
    """
    if not value:
        return "-"
    return value[:width]


def _summary_to_dict(summary: RunSummary) -> dict[str, Any]:
    """Project a :class:`RunSummary` value object into a JSON-friendly dict.

    Pydantic's ``model_dump`` already does the right thing, but going
    through an explicit helper means the JSON-mode and pretty-mode
    paths agree on shape, and adding a derived field (e.g. ``age``)
    later is a single edit.
    """
    return summary.model_dump(mode="json")


def _resolve_run_id(project: Project, run_id: str) -> str:
    """Return the full run id for the value the operator typed.

    Currently this is a flat pass-through that accepts the full UUID
    only — it exists as a function so the eventual prefix-resolution
    helper (TODO once the substrate exposes a prefix-search query) has
    one obvious place to land. We still validate that the id exists so
    every per-run command can rely on the run being present before it
    does any work.
    """
    found = project.get_run(run_id)
    if found is None:
        # NOTE: when prefix support arrives, this is the branch that
        # also handles "prefix matched multiple runs" by raising a
        # different error message; for now the contract is "full UUID
        # or nothing".
        die(
            f"no run with id {run_id!r} "
            "(prefix matching is a TODO; supply the full UUID for now)"
        )
    return run_id


def _print_runs_pretty(rows: list[RunSummary]) -> None:
    """Render ``rows`` as a rich Table on stdout.

    Six columns: short id, short spec id, status, started_at,
    last_update_at, transitions count. We intentionally drop ``ended_at``
    from the default view because most listings include in-flight runs
    where that column is empty — operators can fall back to ``--json``
    when they want every field.
    """
    table = Table(title=f"runs ({len(rows)})", show_lines=False)
    table.add_column("id", style="cyan", no_wrap=True)
    table.add_column("spec_id", style="magenta", no_wrap=True)
    table.add_column("status", style="green")
    table.add_column("started_at")
    table.add_column("last_update_at")
    table.add_column("txns", justify="right")
    for row in rows:
        table.add_row(
            _short_id(row.id),
            _short_id(row.fsm_spec_id),
            row.status,
            row.started_at,
            row.last_update_at,
            str(row.transitions_count),
        )
    rich_print(table)


def _render_state_tree(node: StateNode, prefix: str = "", is_last: bool = True) -> list[str]:
    """Recursively render a state tree as ASCII lines.

    Uses the canonical box-drawing characters (``├──`` / ``└──``) so the
    output is unambiguous even when state names contain underscores
    that would otherwise get confused with hand-rolled separators.

    The line for each node is ``<state_id> [<status>] (seq=<n>)`` so
    the operator can correlate against the events list at a glance —
    the ``state_entered`` / ``state_exited`` event payloads carry the
    same ``entry_seq``.
    """
    connector = "└── " if is_last else "├── "
    label = f"{node.state_id} [{node.status}] (seq={node.entry_seq})"
    lines = [f"{prefix}{connector}{label}" if prefix or not is_last else label]
    # If this is the root call (no prefix), drop the connector so we
    # start with the bare state name rather than a stray ``└──``.
    if not prefix and is_last:
        lines = [label]
    child_prefix = prefix + ("    " if is_last else "│   ")
    children = node.children
    for index, child in enumerate(children):
        last = index == len(children) - 1
        lines.extend(_render_state_tree(child, child_prefix, last))
    return lines


def _state_tree_to_dict(node: StateNode) -> dict[str, Any]:
    """Recursive dict projection of a :class:`StateNode`.

    Pydantic's ``model_dump`` would do this in one call, but
    :class:`StateNode` carries the full ``inputs`` / ``outputs`` JSON
    bags which can be heavy in the JSON-mode summary. The hand-rolled
    projection trims to the fields the ``show`` command actually
    surfaces; callers wanting the full row can still hit the
    individual repo methods.
    """
    return {
        "entry_id": node.entry_id,
        "state_id": node.state_id,
        "entry_seq": node.entry_seq,
        "entered_at": node.entered_at,
        "exited_at": node.exited_at,
        "status": node.status,
        "iteration_n": node.iteration_n,
        "children": [_state_tree_to_dict(child) for child in node.children],
    }


def _ensure_runtime_producer(project: Project) -> str:
    """Upsert the engine producer and return its id.

    Mirrors the lazy upsert performed by :meth:`Project.start_run` so
    the CLI emits events under the same producer identity the engine
    uses at runtime — see :data:`_CLI_PRODUCER_KIND`.
    """
    with project.session_factory() as session, session.begin():
        producer = project.producers.upsert(
            session,
            kind=_CLI_PRODUCER_KIND,
            name=_CLI_PRODUCER_NAME,
        )
    return producer.id


# ---------------------------------------------------------------------------
# ``runs ls``
# ---------------------------------------------------------------------------


@runs_app.command(name="ls", help="List recent FSM runs (optionally filtered).")
def runs_ls(
    db: Path | None = DB_OPTION,
    status: str | None = typer.Option(
        None,
        "--status",
        help=(
            "Filter to runs with this status (e.g. in_progress, paused, "
            "faulted, completed, aborted). When omitted, lists the most "
            "recently updated runs regardless of status."
        ),
    ),
    since: str | None = typer.Option(
        None,
        "--since",
        help=(
            "ISO-8601 lower bound on last_update_at (e.g. "
            "2026-05-01T00:00:00Z). Applied client-side to the filtered "
            "result so it composes with --status."
        ),
    ),
    limit: int = typer.Option(
        20,
        "--limit",
        min=1,
        max=500,
        help="Maximum number of runs to return (default: 20).",
    ),
    json_mode: bool = JSON_OPTION,
) -> None:
    """List recent runs from the project database.

    Routes to the appropriate ``RunsRepo`` accessor based on the
    requested ``--status``:

    * ``incomplete`` / ``resumable`` are recognised as special status
      keywords that map to :meth:`RunsRepo.incomplete` /
      :meth:`RunsRepo.resumable` (matching the substrate's named
      shortcuts so operators don't have to know that "incomplete" is
      really a four-status union).
    * Any other status value goes through :meth:`RunsRepo.by_status`.
    * No status at all hits :meth:`RunsRepo.latest`.

    ``--since`` and ``--limit`` are applied client-side after the
    repo call so the same filtering composes uniformly across all
    three routes.
    """
    db_path = resolve_db_path(db)

    with open_project_for_cli(db_path) as project, project.session_factory() as session:
        if status is None:
            rows = project.runs.latest(session, limit=limit)
        elif status == "incomplete":
            rows = project.runs.incomplete(session)
        elif status == "resumable":
            rows = project.runs.resumable(session)
        else:
            rows = project.runs.by_status(session, status)

    # Apply --since client-side. We compare ISO-8601 strings directly
    # because the substrate writes timestamps with a stable suffix
    # (``+00:00`` for lifecycle tables, ``Z`` for the journal) — both
    # forms sort lexicographically as long as we don't mix shapes.
    if since is not None:
        rows = [row for row in rows if row.last_update_at >= since]

    # Apply the limit after filtering so ``--since`` doesn't silently
    # truncate results the operator asked for.
    rows = rows[:limit]

    if json_mode:
        json_or_pretty([_summary_to_dict(row) for row in rows], json_mode=True)
        return

    if not rows:
        rich_print("[dim]no runs found[/dim]")
        return
    _print_runs_pretty(rows)


# ---------------------------------------------------------------------------
# ``run show``
# ---------------------------------------------------------------------------


@run_app.command(name="show", help="Show a run's manifest, state tree, and recent events.")
def run_show(
    run_id: str = typer.Argument(..., help="Run id (full UUID; prefix support is TODO)."),
    db: Path | None = DB_OPTION,
    json_mode: bool = JSON_OPTION,
) -> None:
    """Print a per-run report: manifest, state tree, last 20 events, journal.

    The report bundles four reads:

    * :meth:`Project.get_run` for the run manifest (status, timestamps,
      args, transitions count, …).
    * :meth:`RunsRepo.state_tree` for the nested state-entry tree.
    * :meth:`RunsRepo.events` (sliced to the most recent 20) for the
      per-run event tail.
    * :meth:`JournalRepo.inspect` for the newest unfinalised journal
      row, if any — that's what the recovery loop would act on.

    JSON output mirrors the same four sections so machine consumers
    don't have to do a second round trip to assemble the same picture.
    """
    db_path = resolve_db_path(db)

    with open_project_for_cli(db_path) as project:
        resolved_id = _resolve_run_id(project, run_id)
        run = project.get_run(resolved_id)
        # ``_resolve_run_id`` already aborts when the run is missing,
        # so this guard is defensive — ``run`` is guaranteed non-None.
        if run is None:  # pragma: no cover — see comment above
            die(f"run vanished mid-show: {resolved_id!r}", code=2)

        with project.session_factory() as session:
            tree = project.runs.state_tree(session, resolved_id)
            # ``events`` is an iterator — materialise it so we can
            # slice to the last 20 and reuse the list for JSON output.
            event_rows = list(project.runs.events(session, resolved_id))
            journal = project.journal.inspect(session, run_id=resolved_id)

    recent_events = event_rows[-20:]

    if json_mode:
        payload: dict[str, Any] = {
            "run": run.model_dump(mode="json"),
            "state_tree": _state_tree_to_dict(tree) if tree is not None else None,
            "recent_events": [event.model_dump(mode="json") for event in recent_events],
            "journal": journal.model_dump(mode="json") if journal is not None else None,
        }
        json_or_pretty(payload, json_mode=True)
        return

    # ── Pretty output ────────────────────────────────────────────────
    rich_print(f"[bold]run[/bold] {run.id}")
    rich_print(f"  [dim]spec[/dim]        {run.fsm_spec_id} (hash={run.fsm_spec_hash[:12]}…)")
    rich_print(f"  [dim]status[/dim]      {run.status}")
    rich_print(f"  [dim]current[/dim]     {run.current_state or '-'}")
    rich_print(f"  [dim]verdict[/dim]     {run.verdict or '-'}")
    rich_print(f"  [dim]started[/dim]     {run.started_at}")
    rich_print(f"  [dim]updated[/dim]     {run.last_update_at}")
    rich_print(f"  [dim]ended[/dim]       {run.ended_at or '-'}")
    rich_print(f"  [dim]transitions[/dim] {run.transitions_count}")

    rich_print("\n[bold]state tree[/bold]")
    if tree is None:
        rich_print("  [dim](no state entries yet)[/dim]")
    else:
        for line in _render_state_tree(tree):
            rich_print(f"  {line}")

    rich_print(f"\n[bold]recent events[/bold] ({len(recent_events)} of {len(event_rows)})")
    if not recent_events:
        rich_print("  [dim](no events recorded)[/dim]")
    else:
        for event in recent_events:
            seq = "-" if event.seq is None else str(event.seq)
            rich_print(
                f"  [cyan]#{seq}[/cyan] [magenta]{event.kind}[/magenta] "
                f"@ {event.created_at} id={_short_id(event.id)}"
            )

    rich_print("\n[bold]journal[/bold]")
    if journal is None:
        rich_print("  [dim](no unfinalised journal txn)[/dim]")
    else:
        rich_print(
            f"  id={_short_id(journal.id)} status={journal.status} "
            f"started={journal.started_at.isoformat()} "
            f"staged_writes={len(journal.staged_writes)}"
        )


# ---------------------------------------------------------------------------
# ``run resume``
# ---------------------------------------------------------------------------


@run_app.command(
    name="resume",
    help="Resume a paused/faulted run (journal bookkeeping; engine resume comes in W12).",
)
def run_resume(
    run_id: str = typer.Argument(..., help="Run id (full UUID; prefix support is TODO)."),
    from_state: str | None = typer.Option(
        None,
        "--from-state",
        help=(
            "Override the resume state. Recorded in the emitted event "
            "but engine-driven resume itself lands in a later workstream (W12)."
        ),
    ),
    journal: str | None = typer.Option(
        None,
        "--journal",
        help=(
            "What to do with the run's unfinalised journal txn: "
            "'discard' deletes it, 'replay' is reserved for W12 (currently a "
            "no-op that records intent). Omit to leave the journal untouched."
        ),
    ),
    db: Path | None = DB_OPTION,
    json_mode: bool = JSON_OPTION,
) -> None:
    """Bookkeeping half of resume.

    In W3 this command does three things:

    1. Optionally acts on the run's open journal txn — ``discard``
       deletes it, ``replay`` records the intent without yet
       re-applying staged writes (the replay-into-engine path is W12).
    2. Emits a structured ``run_resumed`` event so subscribers see the
       operator's intent on the bus.
    3. Prints a one-line notice when ``--from-state`` is supplied,
       making the deferral explicit so operators do not silently
       assume the engine has picked the run back up.

    Returns the same payload via :func:`json_or_pretty` so scripts
    can pin behaviour off the structured event.
    """
    db_path = resolve_db_path(db)

    if journal is not None and journal not in {"discard", "replay"}:
        die(f"--journal must be 'discard' or 'replay' (got {journal!r})")

    with open_project_for_cli(db_path) as project:
        resolved_id = _resolve_run_id(project, run_id)
        producer_id = _ensure_runtime_producer(project)

        journal_action: str | None = None
        journal_txn_id: str | None = None
        with project.session_factory() as session, session.begin():
            existing = project.journal.inspect(session, run_id=resolved_id)
            journal_txn_id = existing.id if existing is not None else None

            if journal == "discard" and existing is not None:
                project.journal.discard(session, txn_id=existing.id)
                journal_action = "discarded"
            elif journal == "replay" and existing is not None:
                # Replay-into-engine lands in W12; here we only record
                # the operator's intent so the event stream tells the
                # full story when the engine wakes back up.
                journal_action = "replay_requested"
            elif journal is not None:
                # The operator asked for a journal action but the run
                # has no unfinalised txn — record that explicitly so
                # the JSON consumer doesn't have to guess.
                journal_action = "noop_no_journal"

            project.events.emit(
                session,
                producer_id=producer_id,
                kind=EventKind.run_resumed.value,
                payload={
                    "run_id": resolved_id,
                    "from_state": from_state,
                    "journal_action": journal_action,
                    "journal_txn_id": journal_txn_id,
                    "engine_resume_pending": True,
                },
                run_id=resolved_id,
            )

    payload: dict[str, Any] = {
        "run_id": resolved_id,
        "from_state": from_state,
        "journal_action": journal_action,
        "journal_txn_id": journal_txn_id,
        "engine_resume": "engine-driven resume comes in a later workstream (W12)",
    }
    json_or_pretty(payload, json_mode)


# ---------------------------------------------------------------------------
# ``run abort``
# ---------------------------------------------------------------------------


@run_app.command(name="abort", help="Mark a run as aborted and emit a run_aborted event.")
def run_abort(
    run_id: str = typer.Argument(..., help="Run id (full UUID; prefix support is TODO)."),
    reason: str | None = typer.Option(
        None,
        "--reason",
        help="Free-text reason recorded on the emitted event payload.",
    ),
    db: Path | None = DB_OPTION,
    json_mode: bool = JSON_OPTION,
) -> None:
    """Atomically mark ``run_id`` as ``aborted`` and emit ``run_aborted``.

    The update and the event emission happen inside one
    ``session.begin()`` block so a crash between them leaves the DB
    consistent (either both rows are visible or neither is) — the same
    guarantee :meth:`Project.start_run` provides for the start side of
    the lifecycle.

    Refuses (with a non-zero exit) when the run is already in a
    terminal state — re-aborting a completed/aborted run would muddy
    the audit trail without changing the user-visible state.
    """
    from ctxr.fsm.sqlite.repos_core import _iso_now_ms  # local import: private helper

    db_path = resolve_db_path(db)

    with open_project_for_cli(db_path) as project:
        resolved_id = _resolve_run_id(project, run_id)
        run = project.get_run(resolved_id)
        if run is None:  # defensive; _resolve_run_id should have aborted
            die(f"run vanished mid-abort: {resolved_id!r}", code=2)
        if run.status in {"completed", "aborted"}:
            die(
                f"run {resolved_id!r} is already in terminal status "
                f"{run.status!r}; refusing to abort"
            )

        producer_id = _ensure_runtime_producer(project)
        now = _iso_now_ms()

        with project.session_factory() as session, session.begin():
            updated = project.runs.update_status(
                session,
                run_id=resolved_id,
                status="aborted",
                ended_at=now,
            )
            if updated is None:
                # update_status returns None when the row disappeared
                # between our pre-check and the write; treat as a hard
                # error so the CLI exits non-zero rather than silently
                # emitting an event for a run that no longer exists.
                die(f"run {resolved_id!r} disappeared mid-abort", code=2)

            project.events.emit(
                session,
                producer_id=producer_id,
                kind=EventKind.run_aborted.value,
                payload={
                    "run_id": resolved_id,
                    "reason": reason,
                    "previous_status": run.status,
                    "ended_at": now,
                },
                run_id=resolved_id,
            )

    payload: dict[str, Any] = {
        "run_id": resolved_id,
        "previous_status": run.status,
        "new_status": "aborted",
        "ended_at": now,
        "reason": reason,
    }
    json_or_pretty(payload, json_mode)
