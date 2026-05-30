"""Meta + bootstrap MCP tools: ``fsm.healthcheck``, ``fsm.list_specs``,
``fsm.register_spec``, ``fsm.observe_tool_call``.

This module groups the four "meta" tools that every MCP client needs
before it can drive a run:

* :func:`fsm_healthcheck` (``fsm.healthcheck``) — the read-only probe
  that satisfies the common-dev *Principle 1* pre-check: a skill calls
  this first to confirm the FSM server is up, the schema is current,
  and the package version matches what it expects. The shape
  intentionally mirrors what ``ctxr-fsm doctor`` surfaces (DB path,
  SQLite version, alembic head) so an operator inspecting the JSON has
  the same facts in both places.

* :func:`fsm_list_specs` (``fsm.list_specs``) — enumerate the FSM
  specifications registered against this database. Used by the UI and
  by orchestrators that need to pick a spec to run; intentionally a
  trimmed summary (no full ``definition`` body) so the response stays
  small even when a project has hundreds of versions.

* :func:`fsm_register_spec` (``fsm.register_spec``) — accept a JSON
  spec, parse it through :meth:`FsmSpec.model_validate_json`, run the
  cross-cutting :func:`validate_fsm_spec` checks, and persist via
  :meth:`Project.register_spec` only if every check passes. Invalid
  specs are *refused* with a structured ``schema_validation_failed``
  / ``invalid_spec_definition`` error envelope so clients can branch on
  the failure rather than parsing free-text.

* :func:`fsm_observe_tool_call` (``fsm.observe_tool_call``) — the
  agent-side hook for layer 7 (drift detection). The contract is
  documented at length in the tool's docstring: every non-``fsm.*``
  tool the agent invokes during an active run should be reported here
  so the drift aggregator sees the timeline. W4 only *records* the
  observation (tool_calls row + ``tool_call_observed`` event); the W12
  enforcement wave will read those rows to compute drift scores.

Error contract
--------------

Every tool wraps its body in ``try/except`` and on failure returns the
legacy ``{"error": "<snake_case>", ...}`` envelope as an
:class:`~ctxr.fsm.mcp._errors.McpToolError` Pydantic model. That keeps
the wire shape identical to the legacy JS server (which clients already
speak) and means the tool never leaks a JSON-RPC error frame to clients
that were built against the old error contract.

Stdout discipline
-----------------

MCP stdio uses *stdout* for JSON-RPC framing; any log line that escapes
to stdout corrupts the protocol stream. We therefore use the module-
scope :data:`_LOG` (configured to stderr by the boot sequence) for
every diagnostic line and rely on the absence of ``print(...)`` here
to keep stdout clean.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid as _uuid_std
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text

from ctxr.fsm import __version__ as _PACKAGE_VERSION  # noqa: N812
from ctxr.fsm.core.models import EventKind, FsmSpec
from ctxr.fsm.core.spec import validate_fsm_spec
from ctxr.fsm.mcp import mcp
from ctxr.fsm.mcp._errors import McpToolError, as_error
from ctxr.fsm.mcp._state import get_project
from ctxr.fsm.sqlite.models_core import FsmSpecTable, ProjectTable

__all__ = [
    "HealthcheckResult",
    "ObserveResult",
    "SpecRegisteredPayload",
    "SpecSummary",
    "fsm_healthcheck",
    "fsm_list_specs",
    "fsm_observe_tool_call",
    "fsm_register_spec",
]


# Per-tool diagnostic logger. The boot sequence pins logging to stderr
# (see :func:`ctxr.fsm.mcp.server._configure_stderr_logging`) so the
# JSON-RPC framing on stdout is never corrupted.
_LOG = logging.getLogger("ctxr.fsm.mcp.tools_meta")


# Pydantic value-objects are frozen so a tool's return value is treated
# as an immutable snapshot of the server state at call time. ``extra=
# "forbid"`` keeps the wire shape closed — clients depending on a
# specific field cannot be silently broken by a typo in a new field.
_VO_CFG = ConfigDict(strict=True, frozen=True, extra="forbid")


# ---------------------------------------------------------------------------
# Pydantic models — typed contract for tool inputs / outputs
# ---------------------------------------------------------------------------


class HealthcheckResult(BaseModel):
    """Read-only probe payload returned by :func:`fsm_healthcheck`.

    Mirrors the subset of ``ctxr-fsm doctor`` fields that a *Principle
    1* pre-check needs to assert a usable environment:

    * ``status`` is always ``"ok"`` on success — clients should branch
      on the presence/absence of an ``error`` key, not on ``status``,
      so the literal string is reserved for the happy path.
    * ``db_path`` is the resolved absolute path of the SQLite file the
      server is bound to. Useful when an operator wants to know which
      database a long-running stdio server is actually writing to.
    * ``sqlite_version`` is the version reported by the underlying
      ``sqlite3`` library (which the CPython build bundles and which
      can drift from the system ``sqlite3`` CLI). Surfaced because
      JSON1 / generated-column support hinges on it.
    * ``alembic_revision`` is the current head as recorded in the
      ``alembic_version`` table. ``None`` when the table is missing
      (which means migrations have never been run — a real bug at this
      point because the server boots with ``migrate=True``, but we
      surface ``None`` rather than crash so the pre-check can report
      it).
    * ``package_version`` is the installed ``ctxr.fsm`` version so
      clients can fail fast on a version mismatch.
    """

    model_config = _VO_CFG

    status: str = Field(default="ok", description="Always 'ok' on success.")
    db_path: str = Field(description="Absolute path of the bound SQLite database.")
    sqlite_version: str = Field(description="SQLite library version (sqlite3.sqlite_version).")
    alembic_revision: str | None = Field(
        default=None,
        description="Current alembic head; None if alembic_version is absent.",
    )
    package_version: str = Field(description="Installed ctxr.fsm package version.")


class SpecSummary(BaseModel):
    """A trimmed row from ``fsm_specs`` joined with its project's slug.

    Returned by :func:`fsm_list_specs` in place of the full
    :class:`~ctxr.fsm.sqlite.RegisteredSpec` so the wire payload stays
    bounded even for projects with hundreds of versions. Callers that
    need the full ``definition`` body should fetch it via a follow-up
    spec-detail tool (W5+) or via the database directly.
    """

    model_config = _VO_CFG

    id: str = Field(description="Row PK of the fsm_specs entry (UUIDv7 string).")
    project_id: str = Field(description="Row PK of the owning project row.")
    project_slug: str = Field(description="Human-readable slug of the owning project.")
    slug: str = Field(description="The FSM's own id, used as the natural slug.")
    version: int = Field(description="Monotonic version within (project_id, slug).")
    hash: str = Field(description="SHA-256 content hash of the canonical spec JSON.")
    created_at: str = Field(description="ISO-8601 timestamp of the registration.")


class SpecRegisteredPayload(BaseModel):
    """Return value of :func:`fsm_register_spec`.

    Mirrors the legacy JS contract: only the *identity* of the
    registered row (``spec_id``, ``hash``, ``version``) plus a boolean
    flag indicating whether a new row was minted vs. an existing
    byte-identical row was returned.

    The full ``RegisteredSpec`` is intentionally *not* embedded — the
    client already has the definition (it just sent it in) and the
    summary fields are all it needs for follow-up calls.
    """

    model_config = _VO_CFG

    spec_id: str = Field(description="Row PK of the registered fsm_specs entry.")
    hash: str = Field(description="SHA-256 canonical hash of the spec.")
    version: int = Field(description="Version assigned within (project, slug).")
    slug: str = Field(description="The FSM id used as the natural slug.")
    project_id: str = Field(description="Row PK of the owning project.")
    project_slug: str = Field(description="Human-readable slug of the owning project.")
    created: bool = Field(
        description="True iff a new row was inserted; False on hash-dedup match."
    )


class ObserveResult(BaseModel):
    """Return value of :func:`fsm_observe_tool_call`.

    ``recorded`` is always ``True`` on the happy path — the field is
    kept so the wire shape remains a positive acknowledgement that the
    drift aggregator (W12) will see the call on its next sweep.

    ``tool_call_id`` and ``event_id`` are surfaced so a debugger or a
    test can correlate the observation across the ``tool_calls`` and
    ``events`` tables without an extra round-trip.
    """

    model_config = _VO_CFG

    recorded: bool = Field(default=True, description="Always True on success.")
    tool_call_id: str = Field(description="Row PK of the inserted tool_calls row.")
    event_id: str = Field(description="Row PK of the emitted tool_call_observed event.")
    producer_id: str = Field(description="Row PK of the upserted producer.")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _alembic_revision_for(engine: Any) -> str | None:
    """Return the current ``alembic_version.version_num`` for ``engine``.

    Mirrors the helper in :mod:`ctxr.fsm.cli.doctor_cmd` so the two
    surfaces report the same value. We swallow exceptions deliberately:
    the table is missing on a freshly-created (un-migrated) database
    and the caller treats ``None`` as "migrations never ran" rather
    than as a hard error — a healthcheck that crashes on a missing
    table is far less useful than one that surfaces the gap.
    """
    try:
        with engine.connect() as conn:
            row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
    except Exception:
        return None
    if row is None:
        return None
    return str(row[0])


def _engine_db_path(engine: Any) -> str:
    """Return a human-readable path for the SQLite engine's database.

    SQLAlchemy ``Engine.url`` is a structured object whose
    ``.database`` attribute is the path the driver was opened against.
    For SQLite that is either an absolute filesystem path or one of
    the in-memory sentinels (``:memory:`` / empty string). We coerce
    to ``str`` so the JSON shape stays stable regardless of which
    case applies.
    """
    db = engine.url.database
    if db is None or db == "":
        return ":memory:"
    return str(db)


# ---------------------------------------------------------------------------
# fsm.healthcheck
# ---------------------------------------------------------------------------


@mcp.tool(
    name="fsm.healthcheck",
    description=(
        "Read-only probe: returns server status, DB path, SQLite version, "
        "alembic head, and the ctxr.fsm package version. Used by skill "
        "pre-checks (common-dev Principle 1) to confirm the FSM substrate "
        "is reachable and current."
    ),
)
def fsm_healthcheck() -> HealthcheckResult | McpToolError:
    """Probe the live MCP server and return a structured snapshot.

    The tool is the canonical pre-check every skill runs before issuing
    any mutating call. It is intentionally cheap — one ``SELECT
    version_num FROM alembic_version`` plus a couple of attribute
    reads — so it can be polled in tight loops without measurable
    overhead.

    Returns an :class:`HealthcheckResult` on success or a structured
    :class:`McpToolError` on failure. The most common failure is
    ``project_not_bound`` (the server booted but ``set_project`` was
    never called), surfaced as a hint for the operator that the boot
    sequence is incomplete.
    """
    try:
        project = get_project()
        engine = project.engine
        db_path = _engine_db_path(engine)
        # ``sqlite3.sqlite_version`` is the C library version the
        # Python build links against — the canonical thing to report
        # in a healthcheck because feature support (JSON1, generated
        # columns, partial indexes) hinges on it.
        sqlite_version = sqlite3.sqlite_version
        alembic_revision = _alembic_revision_for(engine)

        return HealthcheckResult(
            status="ok",
            db_path=db_path,
            sqlite_version=sqlite_version,
            alembic_revision=alembic_revision,
            package_version=_PACKAGE_VERSION,
        )
    except KeyboardInterrupt:  # pragma: no cover - operator-driven interrupt
        raise
    except RuntimeError as exc:
        # ``get_project`` raises RuntimeError when the project handle
        # is unset — surface that as the dedicated ``project_not_bound``
        # code so clients can distinguish "server is up but not
        # configured" from "server is down".
        _LOG.exception("fsm.healthcheck: project handle not bound")
        return as_error("project_not_bound", detail=str(exc))
    except Exception as exc:
        _LOG.exception("fsm.healthcheck: unexpected error")
        return as_error("internal_error", detail=str(exc))


# ---------------------------------------------------------------------------
# fsm.list_specs
# ---------------------------------------------------------------------------


@mcp.tool(
    name="fsm.list_specs",
    description=(
        "List every FSM specification registered against this database, "
        "across all projects, as trimmed summaries (id, project_id, "
        "project_slug, slug, version, hash, created_at). The full "
        "definition body is intentionally omitted."
    ),
)
def fsm_list_specs() -> list[SpecSummary] | McpToolError:
    """Enumerate registered specs as trimmed summaries.

    Joins :class:`FsmSpecTable` with :class:`ProjectTable` so each
    row carries the human-readable ``project_slug`` alongside the
    surrogate ``project_id``. Sorted by ``(project_slug, slug,
    version)`` ascending so a client paging through the list sees a
    deterministic, human-readable order without an extra sort step.

    Returns a possibly-empty list of :class:`SpecSummary` on success
    or a structured :class:`McpToolError` on failure.
    """
    try:
        project = get_project()
        # We join on the project FK to pull the slug in a single query —
        # the alternative (per-spec round-trip to projects) is N+1 and
        # noticeable once a database has a few dozen specs.
        stmt = (
            select(
                FsmSpecTable.id,
                FsmSpecTable.project_id,
                ProjectTable.slug.label("project_slug"),
                FsmSpecTable.slug,
                FsmSpecTable.version,
                FsmSpecTable.hash,
                FsmSpecTable.created_at,
            )
            .join(ProjectTable, FsmSpecTable.project_id == ProjectTable.id)
            .order_by(
                ProjectTable.slug.asc(),
                FsmSpecTable.slug.asc(),
                FsmSpecTable.version.asc(),
            )
        )
        with project.session_factory() as session:
            rows = session.execute(stmt).all()

        return [
            SpecSummary(
                id=row.id,
                project_id=row.project_id,
                project_slug=row.project_slug,
                slug=row.slug,
                version=row.version,
                hash=row.hash,
                created_at=row.created_at,
            )
            for row in rows
        ]
    except KeyboardInterrupt:  # pragma: no cover
        raise
    except RuntimeError as exc:
        _LOG.exception("fsm.list_specs: project handle not bound")
        return as_error("project_not_bound", detail=str(exc))
    except Exception as exc:
        _LOG.exception("fsm.list_specs: unexpected error")
        return as_error("internal_error", detail=str(exc))


# ---------------------------------------------------------------------------
# fsm.register_spec
# ---------------------------------------------------------------------------


@mcp.tool(
    name="fsm.register_spec",
    description=(
        "Parse a JSON-encoded FsmSpec, run cross-cutting validation "
        "(reachability, dangling transitions, loop done_field, predicate "
        "parsability), and register it under the given project. Invalid "
        "specs are refused with a structured error envelope; "
        "byte-identical re-registrations are idempotent (created=False)."
    ),
)
def fsm_register_spec(
    definition_json: str,
    project_slug: str = "default",
) -> SpecRegisteredPayload | McpToolError:
    """Register the supplied FSM spec under ``project_slug``.

    The flow is strictly:

    1. Parse ``definition_json`` via
       :meth:`FsmSpec.model_validate_json`. A Pydantic ValidationError
       surfaces as ``invalid_spec_definition`` with the structured
       error payload attached so clients can render per-field
       complaints.
    2. Run :func:`validate_fsm_spec` for the cross-cutting checks
       (reachability, dangling transitions, loop done-field shape,
       predicate parsability). A failure surfaces as
       ``schema_validation_failed`` with the full
       :class:`FsmValidationResult` attached.
    3. Delegate to :meth:`Project.register_spec` for the actual
       insert-or-dedupe.

    On success returns a :class:`SpecRegisteredPayload` carrying the
    new (or matched) spec's id, hash, version, and the
    project / slug pair plus the ``created`` flag.
    """
    try:
        # ── (1) Pydantic schema validation ────────────────────────────
        try:
            spec = FsmSpec.model_validate_json(definition_json)
        except Exception as exc:
            # Pydantic's ValidationError has a stable ``.errors()`` API
            # but it's not the only thing that can land here (raw JSON
            # decode errors, encoding errors); coerce uniformly.
            errors_payload: list[Any]
            getter = getattr(exc, "errors", None)
            if callable(getter):
                try:
                    errors_payload = list(getter())
                except Exception:  # pragma: no cover - defensive
                    errors_payload = [str(exc)]
            else:
                errors_payload = [str(exc)]
            _LOG.info("fsm.register_spec: pydantic validation failed")
            return as_error(
                "invalid_spec_definition",
                detail="failed to parse FsmSpec from definition_json",
                errors=errors_payload,
            )

        # ── (2) Cross-cutting structural validation ───────────────────
        validation = validate_fsm_spec(spec)
        if not validation.valid:
            _LOG.info(
                "fsm.register_spec: structural validation failed (%d errors)",
                len(validation.errors),
            )
            # ``model_dump(mode='json')`` keeps tuples as lists so the
            # JSON shape on the wire is stable.
            return as_error(
                "schema_validation_failed",
                detail="FsmSpec failed cross-cutting validation",
                validation=validation.model_dump(mode="json"),
            )

        # ── (3) Persist via the project facade ────────────────────────
        project = get_project()
        result = project.register_spec(spec, project_slug=project_slug)

        return SpecRegisteredPayload(
            spec_id=result.spec.id,
            hash=result.spec.hash,
            version=result.spec.version,
            slug=result.spec.slug,
            project_id=result.spec.project_id,
            project_slug=project_slug,
            created=result.created,
        )
    except KeyboardInterrupt:  # pragma: no cover
        raise
    except RuntimeError as exc:
        _LOG.exception("fsm.register_spec: project handle not bound")
        return as_error("project_not_bound", detail=str(exc))
    except Exception as exc:
        _LOG.exception("fsm.register_spec: unexpected error")
        return as_error("internal_error", detail=str(exc))


# ---------------------------------------------------------------------------
# fsm.observe_tool_call
# ---------------------------------------------------------------------------


@mcp.tool(
    name="fsm.observe_tool_call",
    description=(
        "Record an agent-side tool invocation so the W12 drift detector "
        "can observe layer-7 activity. The agent SHOULD call this for "
        "every non-fsm.* tool call made during an active run, passing "
        "the producer kind/name, the tool name, redacted arguments, and "
        "the success flag. W4 only records the observation; W12 wires "
        "enforcement."
    ),
)
def fsm_observe_tool_call(
    producer_kind: str,
    producer_name: str,
    tool_name: str,
    args_redacted: dict[str, Any],
    succeeded: bool = True,
    run_id: UUID | None = None,
) -> ObserveResult | McpToolError:
    """Record a tool-call observation + emit a ``tool_call_observed`` event.

    Contract for callers
    --------------------

    The agent (or any orchestrator driving the FSM) is expected to
    invoke this tool *for every non-``fsm.*`` tool call it makes
    during an active run*. The arguments must be redacted at the
    caller — this server does not know which keys carry secrets and
    will persist whatever it is handed verbatim. ``succeeded`` is the
    boolean outcome the agent observed; the server does not attempt
    to verify it.

    ``producer_kind`` / ``producer_name`` identify the source of the
    call (typically ``("agent", "<agent-name>")`` or
    ``("worker", "<role>")``). The pair is upserted into the
    ``producers`` table so re-using the same identity across calls
    re-uses the same producer id; that is what lets the drift
    aggregator (W12) group calls by source.

    ``run_id`` is optional — a ``None`` value records a "run-less"
    observation, which the drift aggregator already filters out of its
    run-scoped queries. UUIDs are accepted as the typed ``uuid.UUID``
    over the wire and coerced to text for storage so the persistence
    layer never gains a datetime / uuid round-trip dependency.

    Side effects
    ------------

    Inside a single transaction:

    1. The ``(producer_kind, producer_name)`` producer is upserted.
    2. A row is inserted into ``tool_calls`` with the redacted args
       and the success flag.
    3. A ``tool_call_observed`` event is emitted on the bus so any
       subscriber (the W12 drift aggregator, the UI live tail, …)
       sees the observation on its next poll.

    All three writes participate in one ``Session.begin()`` block so
    a crash between any two of them leaves the database consistent.
    """
    try:
        project = get_project()

        # Coerce UUID → 36-char text once, up front, so the storage
        # layer never has to think about uuid types. None stays None
        # so the column accepts NULL for run-less observations.
        run_id_text: str | None = str(run_id) if run_id is not None else None

        with project.session_factory() as session, session.begin():
            producer = project.producers.upsert(
                session,
                kind=producer_kind,
                name=producer_name,
            )

            tool_call = project.tool_calls.record(
                session,
                run_id=run_id_text,
                producer_id=producer.id,
                tool_name=tool_name,
                args_redacted=args_redacted or {},
                succeeded=succeeded,
            )

            # Emit the layer-7 event so subscribers (W12 drift
            # aggregator, dashboards) see this observation in their
            # next poll. Payload mirrors the persisted shape so a
            # downstream consumer never has to cross-reference the
            # ``tool_calls`` table just to read the tool name.
            event = project.events.emit(
                session,
                producer_id=producer.id,
                kind=EventKind.tool_call_observed.value,
                payload={
                    "tool_call_id": tool_call.id,
                    "tool_name": tool_name,
                    "producer_kind": producer_kind,
                    "producer_name": producer_name,
                    "succeeded": bool(succeeded),
                    "args_redacted": args_redacted or {},
                },
                run_id=run_id_text,
            )

        return ObserveResult(
            recorded=True,
            tool_call_id=tool_call.id,
            event_id=event.id,
            producer_id=producer.id,
        )
    except KeyboardInterrupt:  # pragma: no cover
        raise
    except RuntimeError as exc:
        _LOG.exception("fsm.observe_tool_call: project handle not bound")
        return as_error("project_not_bound", detail=str(exc))
    except ValueError as exc:
        # ``UUID`` coercion would have happened in FastMCP's input
        # validation, but downstream value-validation (e.g. an empty
        # ``producer_name``) can still raise here. Surface as the
        # ``invalid_argument`` code so clients can distinguish bad
        # input from server-side bugs.
        _LOG.info("fsm.observe_tool_call: invalid argument")
        return as_error("invalid_argument", detail=str(exc))
    except Exception as exc:
        _LOG.exception("fsm.observe_tool_call: unexpected error")
        return as_error("internal_error", detail=str(exc))


# Re-export the stdlib UUID module under a private name so future
# extensions can adopt it (e.g. ``observe_tool_call`` accepting a
# str-and-uuid union) without touching the import list. Marked
# unused-by-linter via ``_`` prefix; not part of ``__all__``.
_uuid_module = _uuid_std
