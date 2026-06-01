"""Sub-repositories for the W12 enforcement substrate.

This module groups the CRUD + minimal-aggregation surface for the four
tables that make the FSM enforcement shell observable:

* :class:`ToolCallsRepo` — every tool invocation a worker / the engine
  made, captured (with arguments redacted) for post-hoc audit and the
  drift aggregator.
* :class:`DriftSignalsRepo` — typed drift signals (off-allowlist tool
  calls, repeated validation failures, signature mismatches, …) plus a
  ``score_for_run`` aggregator that sums weights.
* :class:`CommitSignaturesRepo` — SHA-256 commitments binding a brief
  to its outputs at commit time, with a ``last_for_run`` lookup for the
  resume / verify path.
* :class:`CommitTokensRepo` — short-lived single-use tokens that
  authorise a state-commit. ``issue`` mints a token, ``consume`` validates
  + atomically marks it consumed, ``expire_stale`` is the reaper.

Design conventions enforced here (see also ``AGENTS.md`` and the W2 brief):

* Every public method accepts a ``sqlalchemy.orm.Session`` as its first
  argument. The session is expected to come from the W2 ``@atomic``
  decorator (or, in tests, a manually opened session wrapped in
  ``session.begin()``). Repositories do **not** open transactions
  themselves — they participate in the ambient unit-of-work.
* Datetime fields are written as ``datetime.now(UTC).isoformat(
  timespec='milliseconds')`` followed by the ``Z`` suffix that matches
  the ``_utc_iso_millis`` shape used by the table-side defaults.
* UUIDs are minted via ``uuid_utils.uuid7()`` and stored as ``str(uuid)``.
* JSON fields are written via ``json.dumps(obj, sort_keys=True,
  separators=(",", ":"))`` so every byte that lands on disk is canonical
  — same input ⇒ same hash ⇒ same row.
* Pydantic value-objects live at the public boundary. SQLModel table
  classes (``ToolCallTable``, ``DriftSignalTable``, …) are private
  implementation details and never appear in method signatures or
  return types.
* The :class:`CommitSignature` Pydantic model from
  ``ctxr.fsm.core.models`` is the canonical shape for a commitment
  envelope, but it does not carry the storage-side bookkeeping
  (``id``, ``run_id``, ``state_id``, ``iteration_n``, ``verified``,
  ``created_at``). We therefore wrap it in a sibling value-object,
  :class:`CommitSignatureRecord`, that pairs the engine-side envelope
  with the persistence-side metadata.
* This module is pure ``ctxr.fsm.sqlite`` — no FastAPI, no MCP, no
  business logic. Just CRUD + the two aggregate queries called out in
  the brief (``DriftSignalsRepo.score_for_run`` and
  ``CommitTokensRepo.expire_stale``).
"""

from __future__ import annotations

import json
import uuid as _uuid_std
from datetime import UTC, datetime, timedelta
from typing import Any

import uuid_utils
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, over, select, update
from sqlalchemy.orm import Session

from ctxr.fsm.core.models import CommitSignature as CoreCommitSignature
from ctxr.fsm.sqlite.models_enforcement import (
    CommitSignatureTable,
    CommitTokenTable,
    DriftSignalTable,
    ToolCallTable,
)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _new_uuid7_str() -> str:
    """Mint a fresh UUIDv7 as the canonical 36-char hyphenated string.

    Repositories use UUIDv7 for every surrogate key they create so the
    row sort order matches insertion order without a separate timestamp
    column lookup.
    """
    return str(uuid_utils.uuid7())


def _utc_iso_millis() -> str:
    """Return ``datetime.now(UTC)`` as ISO-8601 with millisecond precision.

    The shape — ``2026-05-29T12:34:56.789Z`` — matches the
    ``_utc_iso_millis`` helper in ``models_enforcement`` so rows minted
    by repository code are indistinguishable from rows minted by the
    table-side default factory.
    """
    now = datetime.now(tz=UTC)
    # ``isoformat(timespec='milliseconds')`` emits the ``+00:00`` UTC
    # offset; we swap it for the canonical ``Z`` suffix to keep the
    # textual form short and stable.
    base = now.isoformat(timespec="milliseconds")
    if base.endswith("+00:00"):
        return base[:-6] + "Z"
    return base


def _canonical_json(obj: Any) -> str:
    """Serialise ``obj`` to canonical JSON text.

    Canonical here means ``sort_keys=True`` and the most compact
    separators — the same shape ``ctxr.fsm.core.models`` uses when
    computing commit signatures, so a roundtrip
    (Python → JSON → Python → JSON) is byte-stable.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _parse_iso_millis(value: str) -> datetime:
    """Parse the canonical ``YYYY-MM-DDTHH:MM:SS.sssZ`` shape back to UTC datetime.

    Python's stdlib ``fromisoformat`` accepts the ``Z`` suffix from
    3.11+, but we go through a small adapter so older third-party
    serialisers that drop the suffix still roundtrip cleanly.
    """
    if value.endswith("Z"):
        # ``fromisoformat`` understands the ``Z`` literal since 3.11, but
        # being explicit keeps the intent obvious to readers.
        return datetime.fromisoformat(value[:-1] + "+00:00")
    return datetime.fromisoformat(value)


# ---------------------------------------------------------------------------
# Pydantic value-objects (public boundary)
# ---------------------------------------------------------------------------


_VO_CFG = ConfigDict(strict=True, frozen=True, extra="forbid", populate_by_name=True)


class ToolCall(BaseModel):
    """A captured tool invocation row."""

    model_config = _VO_CFG

    id: str
    run_id: str | None
    producer_id: str
    tool_name: str
    args_redacted: dict[str, Any] = Field(default_factory=dict)
    succeeded: bool
    created_at: str


class DriftSignal(BaseModel):
    """A typed drift signal row."""

    model_config = _VO_CFG

    id: str
    run_id: str
    producer_id: str
    signal_kind: str
    weight: float
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class CommitSignatureRecord(BaseModel):
    """Persistence-side view of a commit signature row.

    Pairs the canonical :class:`ctxr.fsm.core.models.CommitSignature`
    envelope (``brief_id``, ``inputs_hash``, ``outputs_hash``,
    ``session_id``, ``signature``) with the storage-side bookkeeping
    (``id``, ``run_id``, ``state_id``, ``iteration_n``, ``verified``,
    ``created_at``).

    Use :meth:`to_core` when you need to recompute / compare against
    the engine-side envelope; use :meth:`from_core_with_meta` to lift
    a freshly computed envelope into the persistence shape.
    """

    model_config = _VO_CFG

    id: str
    run_id: str
    state_id: str
    iteration_n: int | None
    brief_id: str
    inputs_hash: str
    outputs_hash: str
    session_id: str
    signature: str
    verified: bool
    created_at: str

    def to_core(self) -> CoreCommitSignature:
        """Return the core :class:`CommitSignature` envelope view of this row.

        The ``brief_id`` field on the core model is a ``uuid.UUID``;
        we coerce from text here so callers stay in the typed lane.
        """
        return CoreCommitSignature(
            brief_id=_uuid_std.UUID(self.brief_id),
            inputs_hash=self.inputs_hash,
            outputs_hash=self.outputs_hash,
            session_id=self.session_id,
            signature=self.signature,
        )

    @classmethod
    def from_core_with_meta(
        cls,
        envelope: CoreCommitSignature,
        *,
        id: str,
        run_id: str,
        state_id: str,
        iteration_n: int | None,
        verified: bool,
        created_at: str,
    ) -> CommitSignatureRecord:
        """Lift a core envelope + persistence metadata into a record."""
        return cls(
            id=id,
            run_id=run_id,
            state_id=state_id,
            iteration_n=iteration_n,
            brief_id=str(envelope.brief_id),
            inputs_hash=envelope.inputs_hash,
            outputs_hash=envelope.outputs_hash,
            session_id=envelope.session_id,
            signature=envelope.signature,
            verified=verified,
            created_at=created_at,
        )


class CommitTokenRecord(BaseModel):
    """Persistence-side view of a commit-token row.

    The core engine has its own :class:`ctxr.fsm.core.models.CommitToken`
    (with ``token: uuid.UUID`` and ``expires_at: datetime``). The
    persistence shape uses textual forms so both ends remain
    self-consistent — the storage substrate never gains a
    ``datetime`` round-trip dependency.
    """

    model_config = _VO_CFG

    token: str
    run_id: str
    state_id: str
    expected_next_state: str
    expires_at: str
    consumed_at: str | None


class ConsumeResult(BaseModel):
    """Outcome of an attempted :meth:`CommitTokensRepo.consume`.

    ``ok`` is the boolean accept/reject. ``reason`` is a short
    machine-readable slug describing the rejection cause when
    ``ok=False`` (``"not_found"``, ``"already_consumed"``, ``"expired"``,
    ``"state_mismatch"``); ``None`` on success.

    ``token`` is the persisted-side view of the token row as it stands
    after the call — ``consumed_at`` will be populated on success and
    on the ``already_consumed`` rejection path.
    """

    model_config = _VO_CFG

    ok: bool
    reason: str | None = None
    token: CommitTokenRecord | None = None


# ---------------------------------------------------------------------------
# Row → value-object adapters (private)
# ---------------------------------------------------------------------------


def _tool_call_from_row(row: ToolCallTable) -> ToolCall:
    """Decode a ``ToolCallTable`` row into the public :class:`ToolCall` shape."""
    try:
        args = json.loads(row.args_redacted_json) if row.args_redacted_json else {}
    except json.JSONDecodeError:
        # Storage corruption shouldn't crash a read path — surface as
        # an empty bag and let the caller see the raw text via a
        # dedicated debug query if they need it.
        args = {}
    if not isinstance(args, dict):
        args = {}
    return ToolCall(
        id=row.id,
        run_id=row.run_id,
        producer_id=row.producer_id,
        tool_name=row.tool_name,
        args_redacted=args,
        succeeded=bool(row.succeeded),
        created_at=row.created_at,
    )


def _drift_signal_from_row(row: DriftSignalTable) -> DriftSignal:
    """Decode a ``DriftSignalTable`` row into a :class:`DriftSignal`."""
    try:
        payload = json.loads(row.payload_json) if row.payload_json else {}
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return DriftSignal(
        id=row.id,
        run_id=row.run_id,
        producer_id=row.producer_id,
        signal_kind=row.signal_kind,
        weight=float(row.weight),
        payload=payload,
        created_at=row.created_at,
    )


def _commit_signature_from_row(row: CommitSignatureTable) -> CommitSignatureRecord:
    """Decode a ``CommitSignatureTable`` row into a :class:`CommitSignatureRecord`."""
    return CommitSignatureRecord(
        id=row.id,
        run_id=row.run_id,
        state_id=row.state_id,
        iteration_n=row.iteration_n,
        brief_id=row.brief_id,
        inputs_hash=row.inputs_hash,
        outputs_hash=row.outputs_hash,
        session_id=row.session_id,
        signature=row.signature,
        verified=bool(row.verified),
        created_at=row.created_at,
    )


def _commit_token_from_row(row: CommitTokenTable) -> CommitTokenRecord:
    """Decode a ``CommitTokenTable`` row into a :class:`CommitTokenRecord`."""
    return CommitTokenRecord(
        token=row.token,
        run_id=row.run_id,
        state_id=row.state_id,
        expected_next_state=row.expected_next_state,
        expires_at=row.expires_at,
        consumed_at=row.consumed_at,
    )


# ---------------------------------------------------------------------------
# ToolCallsRepo
# ---------------------------------------------------------------------------


class ToolCallsRepo:
    """CRUD over the ``tool_calls`` enforcement table.

    The repository is stateless — every call takes a fresh
    :class:`sqlalchemy.orm.Session`. This makes it cheap to instantiate
    once at engine startup and reuse across threads / async tasks; the
    transactional context is supplied by the caller.
    """

    def record(
        self,
        session: Session,
        *,
        run_id: str | None,
        producer_id: str,
        tool_name: str,
        args_redacted: dict[str, Any],
        succeeded: bool,
    ) -> ToolCall:
        """Persist a single tool-call row.

        ``run_id`` may be ``None`` for tool calls the orchestrator
        emits before opening a run; the drift aggregator already filters
        such rows out of its run-scoped queries.
        """
        row = ToolCallTable(
            id=_new_uuid7_str(),
            run_id=run_id,
            producer_id=producer_id,
            tool_name=tool_name,
            args_redacted_json=_canonical_json(args_redacted or {}),
            succeeded=succeeded,
            created_at=_utc_iso_millis(),
        )
        session.add(row)
        # Flush so the row gets its definitive on-disk shape and any
        # FK / CHECK violations surface to the caller now rather than
        # at commit time. We deliberately do NOT commit — the @atomic
        # decorator owns that.
        session.flush()
        return _tool_call_from_row(row)

    def by_run(
        self,
        session: Session,
        run_id: str,
        *,
        limit: int = 100,
    ) -> list[ToolCall]:
        """Return the most recent ``limit`` tool calls for ``run_id``.

        Sorted by ``created_at DESC`` so the freshest calls come first
        — which lines up with how the drift aggregator walks the
        timeline (newest signal first, stop when score exceeds the
        pause threshold).
        """
        if limit < 1:
            return []
        stmt = (
            select(ToolCallTable)
            .where(ToolCallTable.run_id == run_id)
            .order_by(ToolCallTable.created_at.desc())
            .limit(limit)
        )
        rows = session.execute(stmt).scalars().all()
        return [_tool_call_from_row(row) for row in rows]

    def by_run_paged(
        self,
        session: Session,
        run_id: str,
        *,
        sort_axis: str,
        offset: int,
        limit: int,
    ) -> tuple[list[ToolCall], int]:
        """Paginated per-run tool-call slice + true total.

        W22b2-introduced sibling to :meth:`by_run`. The non-paged
        variant pre-caps with ``limit`` so the HTTP /admin/tool_calls
        handler could never honestly report a population larger than
        the cap — :attr:`Page.total` would lie for any run with more
        than 100 tool calls. This variant runs the same WHERE filter
        with ``COUNT(*) OVER ()`` so the slice and the population
        total come back in one statement.

        ``sort_axis`` accepts ``"created_at_desc"`` (matches
        :meth:`by_run`'s default) or ``"created_at_asc"``.
        """
        if limit < 1:
            return [], 0

        stmt = select(ToolCallTable).where(ToolCallTable.run_id == run_id)
        if sort_axis == "created_at_asc":
            stmt = stmt.order_by(ToolCallTable.created_at.asc())
        else:
            stmt = stmt.order_by(ToolCallTable.created_at.desc())

        count_col = over(func.count()).label("__page_total__")
        paged = stmt.add_columns(count_col).offset(offset).limit(limit)

        rows = list(session.execute(paged))
        if not rows:
            count_stmt = select(func.count()).select_from(stmt.subquery())
            total = int(session.execute(count_stmt).scalar_one())
            return [], total

        total = int(rows[0]._mapping["__page_total__"])
        items = [_tool_call_from_row(row[0]) for row in rows]
        return items, total


# ---------------------------------------------------------------------------
# DriftSignalsRepo
# ---------------------------------------------------------------------------


class DriftSignalsRepo:
    """CRUD over the ``drift_signals`` enforcement table.

    The single aggregate query exposed here, :meth:`score_for_run`, is
    the input the W12 drift aggregator turns into a pause decision.
    """

    def record(
        self,
        session: Session,
        *,
        run_id: str,
        producer_id: str,
        signal_kind: str,
        weight: float,
        payload: dict[str, Any],
    ) -> DriftSignal:
        """Persist a single drift-signal row.

        ``signal_kind`` is expected to be the string value of a
        :class:`ctxr.fsm.core.models.SignalKind` member. We do **not**
        validate against the enum here — that's the producer's job, and
        keeping the check at the producer side preserves the
        decoupling between this module and ``ctxr.fsm.core``.
        """
        row = DriftSignalTable(
            id=_new_uuid7_str(),
            run_id=run_id,
            producer_id=producer_id,
            signal_kind=signal_kind,
            weight=float(weight),
            payload_json=_canonical_json(payload or {}),
            created_at=_utc_iso_millis(),
        )
        session.add(row)
        session.flush()
        return _drift_signal_from_row(row)

    def by_run(self, session: Session, run_id: str) -> list[DriftSignal]:
        """Return every drift signal for ``run_id`` ordered oldest-first.

        Oldest-first ordering matches the natural reading order of a
        timeline and is the shape the aggregator's score-up loop wants
        (it accumulates weights in arrival order so the threshold-cross
        timestamp is recoverable).
        """
        stmt = (
            select(DriftSignalTable)
            .where(DriftSignalTable.run_id == run_id)
            .order_by(DriftSignalTable.created_at.asc())
        )
        rows = session.execute(stmt).scalars().all()
        return [_drift_signal_from_row(row) for row in rows]

    def score_for_run(self, session: Session, run_id: str) -> float:
        """Return the summed ``weight`` of every drift signal for ``run_id``.

        Returns ``0.0`` for runs with no recorded signals (rather than
        ``None``) so the caller can compare directly against a pause
        threshold without a null check.
        """
        stmt = select(func.sum(DriftSignalTable.weight)).where(
            DriftSignalTable.run_id == run_id
        )
        total = session.execute(stmt).scalar()
        if total is None:
            return 0.0
        return float(total)


# ---------------------------------------------------------------------------
# CommitSignaturesRepo
# ---------------------------------------------------------------------------


class CommitSignaturesRepo:
    """CRUD over the ``commit_signatures`` enforcement table.

    A commit signature is recorded at the moment a worker commits its
    outputs for a state (or one iteration of a loop). The single read
    helper, :meth:`last_for_run`, supports the resume path which needs
    to know the most recent successful commit envelope for the run.
    """

    def record(
        self,
        session: Session,
        *,
        run_id: str,
        state_pk: str,
        iteration_n: int | None,
        brief_id: str,
        inputs_hash: str,
        outputs_hash: str,
        session_id: str,
        signature: str,
        verified: bool,
    ) -> CommitSignatureRecord:
        """Persist a commit-signature row.

        ``state_pk`` is the **row primary key** of the corresponding
        ``states`` row (a UUIDv7 string), not the FSM state name. The
        brief/state IDs are kept as text in both the input and the
        stored row so this layer never has to round-trip through
        ``uuid.UUID``.
        """
        row = CommitSignatureTable(
            id=_new_uuid7_str(),
            run_id=run_id,
            state_id=state_pk,
            iteration_n=iteration_n,
            brief_id=brief_id,
            inputs_hash=inputs_hash,
            outputs_hash=outputs_hash,
            session_id=session_id,
            signature=signature,
            verified=verified,
            created_at=_utc_iso_millis(),
        )
        session.add(row)
        session.flush()
        return _commit_signature_from_row(row)

    def last_for_run(
        self, session: Session, run_id: str
    ) -> CommitSignatureRecord | None:
        """Return the most recently created commit-signature row for ``run_id``.

        Uses ``created_at DESC`` (backed by the
        ``idx_commit_signatures_run`` composite index on
        ``(run_id, created_at)``) so this is a cheap index scan.
        Returns ``None`` when the run has no committed signatures yet.
        """
        stmt = (
            select(CommitSignatureTable)
            .where(CommitSignatureTable.run_id == run_id)
            .order_by(CommitSignatureTable.created_at.desc())
            .limit(1)
        )
        row = session.execute(stmt).scalars().first()
        if row is None:
            return None
        return _commit_signature_from_row(row)


# ---------------------------------------------------------------------------
# CommitTokensRepo
# ---------------------------------------------------------------------------


class CommitTokensRepo:
    """CRUD over the ``commit_tokens`` enforcement table.

    The token lifecycle has three operations:

    * :meth:`issue` — mint a new token on state entry.
    * :meth:`consume` — validate-and-consume at commit time. The check
      is strict: token must exist, must not already be consumed, must
      not have expired, and the supplied ``expected_next_state`` must
      match what the token was issued for.
    * :meth:`expire_stale` — the reaper that marks long-expired tokens
      as consumed so the live-token index stays small.
    """

    def issue(
        self,
        session: Session,
        *,
        run_id: str,
        state_id: str,
        expected_next_state: str,
        ttl_seconds: int = 60,
    ) -> CommitTokenRecord:
        """Mint a single-use commit token expiring ``ttl_seconds`` from now.

        ``state_id`` is the FSM state name (snake_case) the engine is
        currently in — the token authorises the **transition out of**
        that state into ``expected_next_state``.

        Raises ``ValueError`` for non-positive TTLs so a misconfigured
        caller fails loudly rather than minting a perpetually-expired
        token.
        """
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be >= 1")

        expires_at_dt = datetime.now(tz=UTC) + timedelta(seconds=ttl_seconds)
        expires_at_iso = expires_at_dt.isoformat(timespec="milliseconds")
        if expires_at_iso.endswith("+00:00"):
            expires_at_iso = expires_at_iso[:-6] + "Z"

        row = CommitTokenTable(
            token=_new_uuid7_str(),
            run_id=run_id,
            state_id=state_id,
            expected_next_state=expected_next_state,
            expires_at=expires_at_iso,
            consumed_at=None,
        )
        session.add(row)
        session.flush()
        return _commit_token_from_row(row)

    def consume(
        self,
        session: Session,
        token: str,
        expected_next_state: str,
    ) -> ConsumeResult:
        """Validate and consume ``token``.

        Returns a :class:`ConsumeResult`:

        * ``ok=True`` iff the token exists, is not consumed, is not
          expired, and matches the supplied ``expected_next_state``.
        * ``ok=False`` with a ``reason`` slug otherwise:

          - ``"not_found"`` — no row with that token id.
          - ``"already_consumed"`` — ``consumed_at`` is already set.
          - ``"expired"`` — ``expires_at`` is in the past.
          - ``"state_mismatch"`` — the token was issued for a
            different ``expected_next_state``.

        On success the row's ``consumed_at`` is set to ``now()`` and
        the returned ``token`` carries that value, so the caller can
        write it onto the journal txn record without a second read.
        """
        row = session.get(CommitTokenTable, token)
        if row is None:
            return ConsumeResult(ok=False, reason="not_found", token=None)

        if row.consumed_at is not None:
            return ConsumeResult(
                ok=False, reason="already_consumed", token=_commit_token_from_row(row)
            )

        now_dt = datetime.now(tz=UTC)
        try:
            expires_dt = _parse_iso_millis(row.expires_at)
        except ValueError:
            # Corrupted expires_at — treat as expired so we never accept
            # a token whose lifetime is indeterminate.
            return ConsumeResult(
                ok=False, reason="expired", token=_commit_token_from_row(row)
            )

        if now_dt > expires_dt:
            return ConsumeResult(
                ok=False, reason="expired", token=_commit_token_from_row(row)
            )

        if row.expected_next_state != expected_next_state:
            return ConsumeResult(
                ok=False,
                reason="state_mismatch",
                token=_commit_token_from_row(row),
            )

        # All gates passed — mark consumed atomically within the
        # ambient transaction and return the post-state view.
        row.consumed_at = _utc_iso_millis()
        session.add(row)
        session.flush()
        return ConsumeResult(ok=True, reason=None, token=_commit_token_from_row(row))

    def expire_stale(self, session: Session) -> int:
        """Mark every expired-but-not-yet-consumed token as consumed.

        Returns the count of rows updated. Used by a periodic reaper
        job so the live-token index (``idx_commit_tokens_expires_at``)
        stays bounded; without this, every issued token would
        accumulate forever once its TTL elapsed.
        """
        now_iso = _utc_iso_millis()
        stmt = (
            update(CommitTokenTable)
            .where(CommitTokenTable.consumed_at.is_(None))
            .where(CommitTokenTable.expires_at < now_iso)
            .values(consumed_at=now_iso)
        )
        result = session.execute(stmt)
        session.flush()
        # SQLAlchemy 2.0 ``Result.rowcount`` is the DB-reported affected
        # row count for an UPDATE; SQLite always populates it.
        rowcount = result.rowcount
        return int(rowcount) if rowcount is not None else 0


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


__all__ = [
    "CommitSignatureRecord",
    "CommitSignaturesRepo",
    "CommitTokenRecord",
    "CommitTokensRepo",
    "ConsumeResult",
    "DriftSignal",
    "DriftSignalsRepo",
    "ToolCall",
    "ToolCallsRepo",
]
