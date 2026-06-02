"""``ctxr-fsm doctor`` — diagnostic dump for the project DB and supervisor.

The doctor command is the operator's first stop when "something looks
off". It opens the project (running migrations, so the schema is
guaranteed current), then surfaces the runtime facts that determine
correctness:

* The resolved DB path and the on-disk file size.
* The SQLite version actually loaded by the Python build (the
  ``sqlite3`` module ships its own copy, which can drift from the
  system library).
* The PRAGMAs we care about — journal_mode, foreign_keys, etc. —
  read live via :func:`detect_journal_state` so we observe the values
  the connect-time listener actually applied.
* The current alembic revision so the operator can correlate against
  the migrations directory.
* The list of user tables in the database plus a per-table row count
  (useful to spot drift between the schema and what the engine has
  populated).
* The :class:`JournalTxnTable` breakdown by status
  (``pending`` / ``ready_to_finalise`` / ``finalised``) so a
  half-finalised commit is visible at a glance.
* The :class:`LockTable` row count so an operator can see whether any
  runs hold the single-writer lock.

W7 service-lifecycle extension
------------------------------

The W7 supervisor persists port assignments and singleton PID files
under ``<project_root>/.ctxr-fsm/`` (``ports.json`` and
``pids/<name>.pid``). The doctor report now surfaces, for each
subsystem (``mcp``, ``api``, ``ui``):

* The remembered port (from ``ports.json``), or ``None`` if no
  ``ctxr-fsm serve`` has booted that subsystem yet.
* The pid recorded in the singleton file (plus the recorded probe URL
  and ``acquired_at`` timestamp), or ``None`` if no pid file exists.
* A liveness flag (``pid_alive``) computed via
  :func:`pid_is_alive` — distinguishes "stale lock" from "running".
* A health-probe outcome (``healthz``) — the body of ``GET
  <probe_url>/healthz`` on success, ``None`` on any failure (no probe
  URL, connection refused, non-200, timeout). Reusing the same
  primitive the supervisor uses means a green doctor report and a
  supervisor "reuse" decision are observing the same signal.

The supervisor section sits alongside the existing DB section in both
the pretty (``rich.print``) and JSON output paths so an operator
chaining ``ctxr-fsm doctor --json | jq`` can pull either domain.

Output is either rich-formatted (default) or pure JSON (``--json``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel
from sqlalchemy import func, select, text

from ctxr.fsm.cli._common import (
    DB_OPTION,
    JSON_OPTION,
    json_or_pretty,
    open_project_for_cli,
    resolve_db_path,
)
from ctxr.fsm.cli._render import portable_project_repr, render_subsystem_table
from ctxr.fsm.cli.lifecycle.primitives import (
    _probe_healthz,
    pid_is_alive,
    read_active_mcp_file,
    read_pid_file,
    recall_port,
)
from ctxr.fsm.memory import (
    get_bootstrap_path,
    get_principles_path,
    get_ssot_doc_path,
    list_ssot_doc_slugs,
)
from ctxr.fsm.sqlite.connection import detect_journal_state
from ctxr.fsm.sqlite.models_core import LockTable

__all__ = ["doctor"]


# The subsystems the supervisor manages. Kept as a module constant so
# the doctor and the supervisor share one source of truth for "which
# names does ``.ctxr-fsm/`` know about" — adding a fourth subsystem
# only needs to touch one place.
_SUPERVISOR_SUBSYSTEMS: tuple[str, ...] = ("mcp", "api", "ui")


def _list_tables(engine: Any) -> list[str]:
    """Return user table names sorted alphabetically.

    We skip ``sqlite_%`` internal tables but include
    ``alembic_version`` (it is user-visible bookkeeping and operators
    expect to see it in the doctor output).
    """
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            )
        ).all()
    return [row[0] for row in rows]


def _count_rows(engine: Any, table_name: str) -> int:
    """Return ``COUNT(*)`` for ``table_name`` using raw SQL.

    We use raw SQL (with a parameter-free, validated identifier from
    :func:`_list_tables`) because the ORM mapping for some bookkeeping
    tables (eg. ``alembic_version``) is not declared in our SQLModel
    modules. The table name is supplied by ``sqlite_master`` so it is
    not user-controlled — there is no SQL-injection vector here.
    """
    with engine.connect() as conn:
        return int(conn.execute(text(f'SELECT COUNT(*) FROM "{table_name}"')).scalar() or 0)


def _alembic_revision(engine: Any) -> str | None:
    """Return ``alembic_version.version_num`` for ``engine`` or ``None``."""
    with engine.connect() as conn:
        try:
            row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
        except Exception:
            return None
    return None if row is None else str(row[0])


def _journal_breakdown(session_factory: Any) -> dict[str, int]:
    """Return per-status counts for the journal_txns table.

    The three statuses match the ``Literal`` declared on
    :class:`ctxr.fsm.sqlite.repos_locks_journal.JournalTxn` so the
    output is stable even if a future migration adds a new status
    we have not accounted for here.
    """
    breakdown = {"pending": 0, "ready_to_finalise": 0, "finalised": 0}
    with session_factory() as session:
        # ``JournalTxnTable.status`` is typed as a bare ``str`` on the
        # SQLModel side (rather than ``Mapped[str]``), so the typed
        # ``select()`` overload does not accept it directly; we use
        # raw SQL here for the same reason the project's repo modules
        # disable the SQLAlchemy mypy overloads — see pyproject.toml.
        rows = session.execute(
            text(
                "SELECT status, COUNT(*) FROM journal_txns GROUP BY status"
            )
        ).all()
    for status, count in rows:
        # Tolerate unknown statuses by stashing them under their own
        # key — better than silently dropping data the operator needs
        # to know about.
        breakdown[str(status)] = int(count)
    return breakdown


def _locks_count(session_factory: Any) -> int:
    """Return the number of rows currently in the locks table."""
    with session_factory() as session:
        return int(
            session.execute(select(func.count()).select_from(LockTable)).scalar() or 0
        )


def _supervisor_subsystem_report(
    name: str, *, project_root: Path
) -> dict[str, Any]:
    """Return the doctor sub-report for one supervisor-managed subsystem.

    Fields:

    * ``name`` — the subsystem name (``mcp`` / ``api`` / ``ui``).
    * ``port`` — the port last remembered via :func:`recall_port`, or
      ``None`` if the supervisor has never booted this subsystem in
      this project.
    * ``pid`` / ``probe_url`` / ``acquired_at`` — fields read straight
      from the singleton pid file (``.ctxr-fsm/pids/<name>.pid``).
      ``None`` when the file is missing or malformed; ``read_pid_file``
      already collapses both cases for us.
    * ``pid_alive`` — :func:`pid_is_alive` result for the recorded pid;
      ``False`` when no pid is recorded so the field stays a strict
      bool (operator scripts can rely on the shape).
    * ``healthz`` — body of ``GET <probe_url>/healthz`` on success,
      otherwise ``None``. We reuse the supervisor's private probe
      helper so a green doctor report matches what the supervisor
      itself observes when deciding whether to reuse an existing
      instance.
    """
    port = recall_port(name, project_root=project_root)

    pid_path = project_root / ".ctxr-fsm" / "pids" / f"{name}.pid"
    pid_record = read_pid_file(pid_path)

    pid: int | None = None
    probe_url: str | None = None
    acquired_at: str | None = None
    if pid_record is not None:
        raw_pid = pid_record.get("pid")
        if isinstance(raw_pid, int):
            pid = raw_pid
        raw_probe = pid_record.get("probe_url")
        if isinstance(raw_probe, str) and raw_probe:
            probe_url = raw_probe
        raw_acquired = pid_record.get("acquired_at")
        if isinstance(raw_acquired, str):
            acquired_at = raw_acquired

    alive = pid_is_alive(pid) if pid is not None else False

    # Health probe is best-effort. We only attempt it when there's a
    # probe URL recorded *and* the pid looks alive — probing a stale
    # URL whose owner is gone would just burn the 1s HTTP timeout for
    # no diagnostic value. ``_probe_healthz`` already swallows every
    # transport error and returns ``None`` for the non-200 case.
    healthz: str | None = None
    if alive and probe_url is not None:
        body = _probe_healthz(probe_url)
        if body is not None:
            healthz = body.strip() or "ok"

    return {
        "name": name,
        "port": port,
        "pid": pid,
        "probe_url": probe_url,
        "acquired_at": acquired_at,
        "pid_alive": alive,
        "healthz": healthz,
    }


def _supervisor_report(project_root: Path) -> dict[str, Any]:
    """Aggregate per-subsystem reports into one stable map.

    Returns ``{"subsystems": {name: report, ...},
    "active_mcp": <discovery-doc> | None}`` so the JSON output can grow
    additional supervisor-level keys (e.g. an ``active_run`` section
    once W12 lands) without breaking the existing
    ``payload["supervisor"]["subsystems"]["mcp"]`` access path.

    The ``active_mcp`` block is the W14c discovery document — verbatim
    if present, ``None`` when no supervisor has booted yet (or the
    last one shut down cleanly). Operators reach for this when a
    skill complains about the HTTP-SSE MCP URL — the doctor's surface
    is the canonical "what URL would the bootstrap fall back to right
    now" answer.
    """
    return {
        "subsystems": {
            name: _supervisor_subsystem_report(name, project_root=project_root)
            for name in _SUPERVISOR_SUBSYSTEMS
        },
        "active_mcp": read_active_mcp_file(project_root),
    }


def _active_mcp_for_table(
    *, supervisor: dict[str, Any]
) -> dict[str, Any]:
    """Translate the doctor's supervisor report into the table input shape.

    The renderer consumes the W14c discovery-document shape
    (``{"subsystems": {<name>: {"http_url", "healthz_url", "pid",
    "status"?}}, ...}``). The doctor's per-subsystem report already
    carries those fields under different keys (``probe_url`` instead
    of ``http_url``, an explicit ``healthz`` body, plus ``pid_alive``).
    This helper bridges the two shapes so the renderer stays pure
    (one input contract, no per-caller adapters).

    Precedence for each block:

    1. The live W14c discovery document if the supervisor wrote one
       (``supervisor["active_mcp"]["subsystems"][name]``) — that
       carries the canonical URLs the supervisor advertised. We layer
       a status word on top derived from the doctor's own probe so
       the colour reflects what doctor just observed (e.g. ``ready``
       vs ``unreachable``) rather than the supervisor's last write.
    2. The doctor's own pid/probe report when no discovery document
       is present (cold supervisor; old project tree). The URL is
       reconstructed from the recorded probe URL so the table is
       still useful — only the ``docs_url`` derivation will trigger
       (api row only).
    """
    active_doc = supervisor.get("active_mcp")
    discovery_subs: dict[str, Any] = {}
    if isinstance(active_doc, dict):
        raw = active_doc.get("subsystems")
        if isinstance(raw, dict):
            discovery_subs = raw

    doctor_subs_raw = supervisor.get("subsystems") or {}
    doctor_subs = doctor_subs_raw if isinstance(doctor_subs_raw, dict) else {}

    merged: dict[str, dict[str, Any]] = {}
    for name in ("mcp", "api", "ui"):
        report = doctor_subs.get(name)
        if not isinstance(report, dict):
            continue
        pid_alive = bool(report.get("pid_alive"))
        healthz_body = report.get("healthz")
        # Decision tree for the status word:
        # * pid down → ``missing``
        # * pid up + healthz ok → ``ready``
        # * pid up + healthz None for a subsystem that *should* probe
        #   → ``unreachable``
        # * UI (no /healthz) with a live pid → ``ready`` (the
        #   supervisor's own contract — see the lifecycle module).
        if not pid_alive:
            status = "missing"
        elif name == "ui" or healthz_body is not None:
            status = "ready"
        else:
            status = "unreachable"

        # Start from the discovery block if we have one — those
        # fields are the supervisor's own publication. Fall back to
        # the doctor's probe URL for the cold-supervisor case.
        discovery_block = discovery_subs.get(name)
        if isinstance(discovery_block, dict):
            block = dict(discovery_block)
        else:
            probe_url = report.get("probe_url")
            block = {
                "http_url": probe_url if isinstance(probe_url, str) else "",
                "healthz_url": (
                    f"{probe_url.rstrip('/')}/healthz"
                    if isinstance(probe_url, str) and probe_url and name != "ui"
                    else None
                ),
                "pid": report.get("pid"),
            }
        block["status"] = status
        merged[name] = block
    return {"subsystems": merged}


def _skill_consumer_report() -> dict[str, Any]:
    """W23-SSOT: report on the canonical reference docs reachability.

    A skill consumer asks: "is everything I need to drive an FSM run
    actually present in this install?" The answer is yes when each of
    the four pillar docs (principles, bootstrap, AGENT_QUICKSTART,
    SKILL_TEMPLATE, GATE_CONTRACT) is readable inside the installed
    package. We resolve each via the public ``ctxr.fsm.memory``
    helpers so this check exercises the SAME code path skills use at
    runtime, catching a stale install rather than a stale duplicate
    constant.

    Returned shape (designed for the JSON wire path)::

        {
          "status": "ok" | "missing_docs",
          "principles_path": str,
          "bootstrap_path": str,
          "ssot_docs": {<slug>: {"path": str, "exists": bool}, ...},
          "missing": [<slug>, ...],   # slugs only, e.g. "principles",
                                      # "bootstrap", "agent_quickstart"
        }

    The ``missing`` list is uniform: every entry is a short slug, not a
    filename. Skills script against this list (``if "agent_quickstart"
    in section["missing"]: ...``) so mixing slug + filename vocabularies
    would make consumers special-case the "principles" / "bootstrap"
    entries. The two non-SSOT pillars use the slugs ``"principles"`` and
    ``"bootstrap"``; the SSOT pillars use whatever
    :func:`list_ssot_doc_slugs` returns (``"agent_quickstart"``,
    ``"skill_template"``, ``"gate_contract"``).
    """

    missing: list[str] = []
    out: dict[str, Any] = {
        "principles_path": None,
        "bootstrap_path": None,
        "ssot_docs": {},
    }

    try:
        out["principles_path"] = str(get_principles_path("claude"))
    except FileNotFoundError:
        missing.append("principles")
    try:
        out["bootstrap_path"] = str(get_bootstrap_path())
    except FileNotFoundError:
        missing.append("bootstrap")

    for slug in list_ssot_doc_slugs():
        entry: dict[str, Any] = {"path": None, "exists": False}
        try:
            path = get_ssot_doc_path(slug)
        except FileNotFoundError:
            missing.append(slug)
        else:
            entry["path"] = str(path)
            entry["exists"] = True
        out["ssot_docs"][slug] = entry

    out["status"] = "ok" if not missing else "missing_docs"
    out["missing"] = missing
    return out


def _print_pretty_report(
    *,
    db_path: Path,
    revision: str | None,
    supervisor: dict[str, Any],
    supervisor_root: Path,
    skill_consumer: dict[str, Any] | None = None,
) -> None:
    """Render the W14j Rich panel + subsystem table to stdout.

    The panel is a two-line summary (DB path + alembic revision) so
    the operator's first read confirms which DB this report describes
    and that migrations are current. The table below sources its
    rows from :func:`_active_mcp_for_table` so the column shape is
    byte-identical to ``ctxr-fsm ensure`` and the supervisor banner.

    When ``skill_consumer`` is provided (operator passed
    ``--skill-consumer``), a one-line readiness summary is printed
    underneath the subsystem table so the pretty surface mirrors the
    information already in the ``--json`` envelope. Without this the
    flag would be a silent no-op outside JSON mode — surfacing a stale
    install only when an operator happens to inspect the JSON.
    """
    console = Console()
    db_repr = portable_project_repr(db_path)
    revision_line = revision or "(no alembic_version row)"
    panel = Panel.fit(
        f"[bold]DB[/bold]       {db_repr}\n"
        f"[bold]Revision[/bold] {revision_line}",
        title="ctxr-fsm doctor",
        title_align="left",
        border_style="cyan",
    )
    console.print(panel)
    console.print(
        render_subsystem_table(
            _active_mcp_for_table(supervisor=supervisor),
            project_root=supervisor_root,
        )
    )
    if skill_consumer is not None:
        # Total pillars = principles + bootstrap + every SSOT slug. We
        # count from the section payload itself (rather than re-deriving)
        # so the line stays in sync if the SSOT slug list grows.
        ssot_docs = skill_consumer.get("ssot_docs") or {}
        total = 2 + len(ssot_docs)
        missing = skill_consumer.get("missing") or []
        resolved = total - len(missing)
        status = skill_consumer.get("status") or "unknown"
        if status == "ok":
            console.print(
                f"[green]skill_consumer:[/green] OK "
                f"({resolved}/{total} pillars resolved)"
            )
        else:
            slug_list = ", ".join(missing) if missing else "(unknown)"
            console.print(
                f"[red]skill_consumer:[/red] missing_docs "
                f"({resolved}/{total} pillars resolved; missing: {slug_list})"
            )


def doctor(
    db: Path | None = DB_OPTION,
    json_mode: bool = JSON_OPTION,
    skill_consumer: bool = typer.Option(
        False,
        "--skill-consumer",
        help=(
            "Add a W23-SSOT 'skill consumer readiness' section to the "
            "report. Verifies that the canonical reference docs "
            "(AGENT_QUICKSTART, SKILL_TEMPLATE, GATE_CONTRACT, "
            "principles, bootstrap) are reachable in the installed "
            "package. Skills that depend on ctxr-fsm should call "
            "this in CI to catch a stale install before a real run "
            "discovers it."
        ),
    ),
) -> None:
    """Print a diagnostic report for the project DB and supervisor.

    Opens the project (running migrations so the report describes a
    fully-current schema), assembles the DB report, then layers on the
    supervisor section (port assignments, PID state, health probes for
    each managed subsystem) and prints the lot via
    :func:`json_or_pretty`.

    The supervisor section is computed against the *current working
    directory* (matching how the supervisor itself resolves its
    ``project_root`` default). An operator running ``ctxr-fsm doctor``
    from a different checkout will see that checkout's ``.ctxr-fsm/``
    state, which is the answer they almost certainly wanted.
    """
    db_path = resolve_db_path(db)
    file_size = db_path.stat().st_size if db_path.exists() else 0

    with open_project_for_cli(db_path) as project:
        pragmas = detect_journal_state(project.engine)
        tables = _list_tables(project.engine)
        row_counts = {name: _count_rows(project.engine, name) for name in tables}
        revision = _alembic_revision(project.engine)
        journal = _journal_breakdown(project.session_factory)
        locks = _locks_count(project.session_factory)

    # The supervisor's lifecycle state lives under
    # ``<cwd>/.ctxr-fsm/`` — the same root the supervisor uses when no
    # explicit ``project_root`` is passed. Resolving here (rather than
    # relative to ``db_path``) means a developer who points ``--db`` at
    # a sibling checkout still sees the *local* supervisor state, which
    # is the answer they almost certainly wanted.
    supervisor_root = Path.cwd().resolve()
    supervisor = _supervisor_report(supervisor_root)

    report: dict[str, Any] = {
        "db_path": str(db_path),
        "file_size_bytes": file_size,
        "sqlite_version": pragmas.get("sqlite_version"),
        "pragmas": {k: v for k, v in pragmas.items() if k != "sqlite_version"},
        "alembic_revision": revision,
        "tables": row_counts,
        "journal_txns": journal,
        "locks": {"count": locks},
        "supervisor": supervisor,
    }
    if skill_consumer:
        report["skill_consumer"] = _skill_consumer_report()
    if json_mode:
        # JSON path is the wire contract every script depends on; we
        # MUST emit the same shape every previous doctor invocation did.
        json_or_pretty(report, json_mode)
        return

    # W14j: human-facing pretty-print is a Rich Panel (DB summary) +
    # the shared subsystem table. This REPLACES the old free-form
    # ``rich.print`` of the report dict — the dict-dump was useful
    # for the early-W7 debugging window but is noisy now that the
    # supervisor surface is the operator's first read. ``--json``
    # callers still get the full report (see the early-return above).
    _print_pretty_report(
        db_path=db_path,
        revision=revision,
        supervisor=supervisor,
        supervisor_root=supervisor_root,
        skill_consumer=report.get("skill_consumer") if skill_consumer else None,
    )
