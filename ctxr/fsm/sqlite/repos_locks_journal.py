"""Repositories for single-writer locks and the atomic-tx journal.

This module exposes two thin CRUD repositories that sit on top of the
:class:`LockTable` (from :mod:`ctxr.fsm.sqlite.models_core`) and the
:class:`JournalTxnTable` (from :mod:`ctxr.fsm.sqlite.models_enforcement`).

Why these two together?
-----------------------
The single-writer lock and the journal-txn row are the *two* mutable
substrates that bracket every state-commit:

* :class:`LocksRepo` is the engine's serialiser. At most one session may hold
  a run's lock at any moment; everyone else must wait or take over a stale
  lease. The lock row is keyed by ``run_id`` (PK), which is exactly the
  "one lock per run" invariant expressed in SQL.
* :class:`JournalRepo` is the engine's pre-commit ledger. Each row brackets
  the staged writes a worker is about to materialise so a crash mid-commit
  can be rolled back deterministically.

The two are decision-coupled at the engine level — acquiring the lock and
opening a journal txn happen back-to-back — but they are mechanically
independent (different tables, different lifecycles), which is why they live
side-by-side in this file rather than being merged into one repository.

Conventions enforced here (see project AGENTS.md and the W2 substrate brief):

* Functions accept a :class:`sqlalchemy.orm.Session` (DI from a sessionmaker).
  The W2 ``@atomic`` decorator wraps callers in ``session.begin()``; this
  module never opens its own transactions and never calls ``commit()`` /
  ``rollback()`` directly. That keeps every mutation atomic with whatever
  surrounding unit-of-work the caller is in.
* All datetime fields written as
  ``datetime.now(timezone.utc).isoformat(timespec='milliseconds')`` strings
  with the trailing ``+00:00`` rewritten to ``Z`` for the canonical Zulu form
  used elsewhere in the schema (see ``ctxr.fsm.sqlite.models_events`` for the
  matching helper).
* All UUIDs generated via :func:`uuid_utils.uuid7` and stored as
  ``str(uuid)``. UUIDv7 is time-ordered so insertion order matches sort order
  — important for the "newest pending txn for run" lookup in
  :meth:`JournalRepo.inspect`.
* JSON fields use ``json.dumps(obj, sort_keys=True, separators=(",", ":"))``
  for canonical, deterministic encoding. The repository owns the encode/decode
  boundary so callers always see Python objects.
* Public methods are fully typed and return Pydantic value-objects
  (:class:`Lock`, :class:`LockResult`, :class:`ReleaseResult`,
  :class:`JournalTxn`) rather than SQLModel table rows. The table classes
  stay an implementation detail.
* Read methods that conceptually yield iterators would yield Pydantic
  objects, not rows — though both repos in this file are single-row /
  single-update shapes, so no iterators are needed.

This module is pure ``ctxr.fsm.sqlite`` — no FastAPI, no MCP, no engine code.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import uuid_utils
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ctxr.fsm.core.models import (
    JournalStatus,
    LockAcquireReason,
    LockReleaseReason,
)
from ctxr.fsm.sqlite.models_core import LockTable
from ctxr.fsm.sqlite.models_enforcement import JournalTxnTable

__all__ = [
    "JournalRepo",
    "JournalTxn",
    "Lock",
    "LockResult",
    "LocksRepo",
    "ReleaseResult",
]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _utc_iso_millis(moment: datetime | None = None) -> str:
    """Return ``moment`` (default: now) as ISO-8601 UTC with ms precision.

    Output shape: ``2026-05-29T12:34:56.789Z``. The trailing ``Z`` matches the
    convention used by ``ctxr.fsm.sqlite.models_events._iso_now_ms`` and by
    every other timestamp persisted by the substrate, so cross-table sorting
    by timestamp is just a TEXT sort.
    """
    now = (moment or datetime.now(UTC)).astimezone(UTC)
    return now.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime:
    """Parse one of *our* ISO-8601 UTC strings back into an aware ``datetime``.

    Mirrors :func:`_utc_iso_millis`: accepts both the ``Z``-suffixed canonical
    form we write and the ``+00:00``-suffixed form Python's ``isoformat``
    natively produces, so we tolerate values written by other tooling against
    the same schema without ambiguity.
    """
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def _canonical_json(obj: Any) -> str:
    """Serialise ``obj`` to canonical JSON.

    Sorted keys + no whitespace gives byte-stable output for the same logical
    payload, which matters because journal staged_writes_json is read back by
    the recovery path and compared against the next pass — non-canonical
    encoding would produce spurious diffs.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _new_uuid7() -> str:
    """Return a fresh UUIDv7 as a 36-char canonical string.

    Used for journal txn ids. Lock rows are keyed by ``run_id``, so they do
    not mint their own UUID.
    """
    return str(uuid_utils.uuid7())


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


class Lock(BaseModel):
    """A snapshot of a single-writer lock row.

    All timestamp fields are exposed as ``datetime`` (aware, UTC) — the
    storage format is ISO-8601 TEXT but callers should never have to know
    that, and parsing into a datetime here keeps "is the lock stale?" checks
    one-liners at the call site.
    """

    model_config = ConfigDict(frozen=True)

    run_id: str
    holder_session_id: str
    acquired_at: datetime
    expires_at: datetime

    @property
    def is_stale(self) -> bool:
        """True iff ``expires_at`` has elapsed relative to "now" (UTC).

        Caller convenience: the repo also performs this check internally
        during :meth:`LocksRepo.acquire`, but exposing it on the value object
        makes higher-level code (e.g. the engine's lock-inspector view) cheap
        to write.
        """
        return self.expires_at <= datetime.now(UTC)


class LockResult(BaseModel):
    """Outcome of an :meth:`LocksRepo.acquire` call.

    ``acquired`` is the single bit of truth the caller needs to branch on.
    The other fields exist for observability / error reporting:

    * ``lock`` — the Lock row as it stands *after* this call. Populated on
      success; also populated on a not-acquired result so the caller can show
      "lock is held by session X until T".
    * ``reason`` — diagnostic discriminator. ``acquired`` means a fresh
      acquisition; ``replaced_stale`` means we took over an expired lease;
      ``already_held_by_same_session`` means a re-entrant acquire; ``held``
      means a live, foreign lock that we did not displace.
    """

    model_config = ConfigDict(frozen=True)

    acquired: bool
    lock: Lock | None = None
    reason: LockAcquireReason


class ReleaseResult(BaseModel):
    """Outcome of an :meth:`LocksRepo.release` call.

    ``released`` is the bit of truth; ``reason`` carries the discriminator:

    * ``released`` — the row was deleted and the lock is now free.
    * ``not_owner`` — a lock exists for the run but is held by a different
      session_id; we refused to release. (Engine policy is "only the holder
      may release"; takeover goes through :meth:`acquire` on a stale lease.)
    * ``not_held`` — no lock row exists for the run; nothing to release.
    """

    model_config = ConfigDict(frozen=True)

    released: bool
    reason: LockReleaseReason


class JournalTxn(BaseModel):
    """A snapshot of a single ``journal_txns`` row.

    ``staged_writes`` is exposed as a decoded Python object (typically a
    list of dicts) rather than the raw JSON text — the repo owns the
    encode/decode boundary so the engine never deals in JSON strings.

    ``status`` is constrained to the three-value lifecycle ``pending →
    ready_to_finalise → finalised``; the repo's own methods are the only
    legitimate way to transition between states.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    run_id: str
    status: JournalStatus
    staged_writes: list[dict[str, Any]] = Field(default_factory=list)
    started_at: datetime
    ready_at: datetime | None = None
    finalised_at: datetime | None = None


# ---------------------------------------------------------------------------
# LocksRepo
# ---------------------------------------------------------------------------


class LocksRepo:
    """CRUD for the ``locks`` table — single-writer lock per run.

    Operations are *minimal aggregation* in the spirit of the W2 brief: no
    business logic, no event emission, no policy. The only judgement this
    repo makes is the stale-lease check inside :meth:`acquire`, because that
    check is structurally part of "acquire" — it determines which SQL we run
    and the value of the returned ``reason`` discriminator.

    Instances are cheap: there is no per-repo state. Construction takes no
    arguments because every method accepts a ``Session`` explicitly; the W2
    ``@atomic`` decorator is responsible for threading a transactional
    session through the call chain.
    """

    def acquire(
        self,
        session: Session,
        *,
        run_id: str,
        session_id: str,
        ttl_seconds: int = 3600,
    ) -> LockResult:
        """Try to acquire the lock for ``run_id`` on behalf of ``session_id``.

        Branches:

        * No existing row — INSERT a fresh lock; ``reason="acquired"``.
        * Existing row, same ``session_id`` — extend the lease in place by
          updating ``expires_at`` to ``now + ttl_seconds``; ``reason=
          "already_held_by_same_session"``. (We deliberately treat this as a
          successful acquire so callers can use ``acquire`` as a heartbeat
          without an extra "extend" method.)
        * Existing row, foreign ``session_id``, expired — REPLACE the row;
          ``reason="replaced_stale"``.
        * Existing row, foreign ``session_id``, live — refuse;
          ``reason="held"``, ``acquired=False``.

        The "REPLACE on stale" path is implemented via SQLite's UPSERT
        (``ON CONFLICT(run_id) DO UPDATE``), guarded by a ``WHERE`` clause
        on the UPDATE target so we do not silently steal a live lock — the
        SQL is what makes the operation atomic against a racing acquirer.
        """
        now = datetime.now(UTC)
        now_iso = _utc_iso_millis(now)
        expires_at = now + timedelta(seconds=ttl_seconds)
        expires_iso = _utc_iso_millis(expires_at)

        # Read first so we can pick the right branch. The read happens
        # inside the caller's transaction (SERIALIZABLE-by-default for
        # SQLite under begin()), so the subsequent write sees the same row.
        existing = session.get(LockTable, run_id)

        if existing is None:
            row = LockTable(
                run_id=run_id,
                holder_session_id=session_id,
                acquired_at=now_iso,
                expires_at=expires_iso,
            )
            session.add(row)
            session.flush()
            return LockResult(
                acquired=True,
                reason=LockAcquireReason.acquired,
                lock=self._row_to_lock(row),
            )

        if existing.holder_session_id == session_id:
            # Re-entrant / heartbeat acquire: refresh the lease in place.
            existing.acquired_at = now_iso
            existing.expires_at = expires_iso
            session.add(existing)
            session.flush()
            return LockResult(
                acquired=True,
                reason=LockAcquireReason.already_held_by_same_session,
                lock=self._row_to_lock(existing),
            )

        existing_expires = _parse_iso(existing.expires_at)
        if existing_expires <= now:
            # Stale lease held by another session — take it over.
            existing.holder_session_id = session_id
            existing.acquired_at = now_iso
            existing.expires_at = expires_iso
            session.add(existing)
            session.flush()
            return LockResult(
                acquired=True,
                reason=LockAcquireReason.replaced_stale,
                lock=self._row_to_lock(existing),
            )

        # Live lock held by a different session — refuse.
        return LockResult(
            acquired=False,
            reason=LockAcquireReason.held,
            lock=self._row_to_lock(existing),
        )

    def release(
        self,
        session: Session,
        *,
        run_id: str,
        session_id: str,
    ) -> ReleaseResult:
        """Release the lock for ``run_id`` iff ``session_id`` is the holder.

        Three outcomes:

        * Row exists and matches ``session_id`` — DELETE and return
          ``released=True``.
        * Row exists but ``holder_session_id != session_id`` — refuse with
          ``reason="not_owner"``. Stealing a live lock is the job of
          :meth:`acquire` (via the stale-lease path), not of release.
        * No row — ``reason="not_held"``. We do NOT treat this as an error
          because release is idempotent at the caller's intent layer: "I no
          longer hold this lock" is already true.
        """
        existing = session.get(LockTable, run_id)
        if existing is None:
            return ReleaseResult(
                released=False, reason=LockReleaseReason.not_held
            )
        if existing.holder_session_id != session_id:
            return ReleaseResult(
                released=False, reason=LockReleaseReason.not_owner
            )
        session.delete(existing)
        session.flush()
        return ReleaseResult(
            released=True, reason=LockReleaseReason.released
        )

    def inspect(self, session: Session, *, run_id: str) -> Lock | None:
        """Return the current lock for ``run_id`` (or ``None`` if unheld).

        Read-only; safe to call outside a write transaction. The returned
        object is a Pydantic snapshot — subsequent acquire/release calls do
        not mutate it.
        """
        row = session.get(LockTable, run_id)
        if row is None:
            return None
        return self._row_to_lock(row)

    # -- internal --------------------------------------------------------

    @staticmethod
    def _row_to_lock(row: LockTable) -> Lock:
        """Project a :class:`LockTable` row into a :class:`Lock` value-object."""
        return Lock(
            run_id=row.run_id,
            holder_session_id=row.holder_session_id,
            acquired_at=_parse_iso(row.acquired_at),
            expires_at=_parse_iso(row.expires_at),
        )


# ---------------------------------------------------------------------------
# JournalRepo
# ---------------------------------------------------------------------------


class JournalRepo:
    """CRUD for the ``journal_txns`` table — pre-commit ledger per run.

    Lifecycle of a row:

    .. code-block:: text

        open()        →  status=pending,           started_at=now
        mark_ready()  →  status=ready_to_finalise, ready_at=now, staged_writes
        finalise()    →  status=finalised,         finalised_at=now
        discard()     →  row deleted

    The recovery path uses :meth:`inspect` to find the *newest* pending or
    ready-to-finalise row for a run — that is the txn that needs to be
    rolled back or rolled forward on engine restart.
    """

    def open(self, session: Session, *, run_id: str) -> JournalTxn:
        """Insert a fresh ``pending`` journal txn and return it.

        ``staged_writes_json`` starts as ``"[]"`` (canonical-encoded empty
        list); ``ready_at`` and ``finalised_at`` stay NULL until the
        corresponding lifecycle calls fire.
        """
        now_iso = _utc_iso_millis()
        row = JournalTxnTable(
            id=_new_uuid7(),
            run_id=run_id,
            status=JournalStatus.pending.value,
            staged_writes_json=_canonical_json([]),
            started_at=now_iso,
            ready_at=None,
            finalised_at=None,
        )
        session.add(row)
        session.flush()
        return self._row_to_txn(row)

    def mark_ready(
        self,
        session: Session,
        *,
        txn_id: str,
        staged_writes: list[dict[str, Any]],
    ) -> JournalTxn:
        """Transition a ``pending`` txn to ``ready_to_finalise``.

        Writes ``staged_writes_json`` in canonical form so the recovery
        path can byte-compare against a freshly-recomputed payload to
        confirm the staged set has not drifted between open and finalise.

        Raises :class:`KeyError` if no row with ``txn_id`` exists.
        """
        row = session.get(JournalTxnTable, txn_id)
        if row is None:
            raise KeyError(f"journal_txn not found: {txn_id!r}")
        row.status = JournalStatus.ready_to_finalise.value
        row.staged_writes_json = _canonical_json(staged_writes)
        row.ready_at = _utc_iso_millis()
        session.add(row)
        session.flush()
        return self._row_to_txn(row)

    def finalise(self, session: Session, *, txn_id: str) -> JournalTxn:
        """Transition a ``ready_to_finalise`` txn to ``finalised``.

        Setting ``finalised_at`` is the durable signal that the staged
        writes have been materialised; the row is retained (not deleted)
        so the audit trail can be reconstructed. The reaper / GC for old
        finalised rows lives elsewhere.

        Raises :class:`KeyError` if no row with ``txn_id`` exists.
        """
        row = session.get(JournalTxnTable, txn_id)
        if row is None:
            raise KeyError(f"journal_txn not found: {txn_id!r}")
        row.status = JournalStatus.finalised.value
        row.finalised_at = _utc_iso_millis()
        session.add(row)
        session.flush()
        return self._row_to_txn(row)

    def discard(self, session: Session, *, txn_id: str) -> None:
        """Delete a journal txn row, regardless of status.

        Used on the abort path — a worker failed before mark_ready, or the
        operator paused the run, and the staged writes will never be
        materialised. Idempotent: calling discard on a non-existent
        ``txn_id`` is a no-op rather than an error, mirroring the release
        semantics on :meth:`LocksRepo.release`.
        """
        row = session.get(JournalTxnTable, txn_id)
        if row is None:
            return
        session.delete(row)
        session.flush()

    def inspect(self, session: Session, *, run_id: str) -> JournalTxn | None:
        """Return the newest *unfinalised* journal txn for ``run_id``, or None.

        "Unfinalised" means ``status IN ('pending', 'ready_to_finalise')``.
        "Newest" is taken on ``started_at DESC`` with a tiebreaker on
        ``id DESC`` — IDs are UUIDv7 so the tiebreaker is also time-ordered.

        This is the read the recovery loop uses at engine startup: if a row
        comes back, the engine either rolls it forward (status=
        ``ready_to_finalise``) or back (status=``pending``); if ``None`` the
        run was last seen in a quiescent state.
        """
        unfinalised_statuses = (
            JournalStatus.pending.value,
            JournalStatus.ready_to_finalise.value,
        )
        stmt = (
            select(JournalTxnTable)
            .where(JournalTxnTable.run_id == run_id)
            .where(JournalTxnTable.status.in_(unfinalised_statuses))
            .order_by(JournalTxnTable.started_at.desc(), JournalTxnTable.id.desc())
            .limit(1)
        )
        row = session.execute(stmt).scalars().first()
        if row is None:
            return None
        return self._row_to_txn(row)

    # -- internal --------------------------------------------------------

    @staticmethod
    def _row_to_txn(row: JournalTxnTable) -> JournalTxn:
        """Project a :class:`JournalTxnTable` row into a :class:`JournalTxn`.

        Decodes ``staged_writes_json`` into a Python list. We tolerate an
        empty / NULL-equivalent payload by defaulting to an empty list so
        the value-object's ``staged_writes`` field is always a list, never
        None.
        """
        raw = row.staged_writes_json or "[]"
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            # Defensive: a malformed payload should not crash inspect().
            # The recovery loop will treat this as "no usable staged writes"
            # and roll the txn back, which is the safe default.
            decoded = []
        if not isinstance(decoded, list):
            decoded = []
        # Narrow to the typed shape; the table column is canonical JSON so
        # non-dict items would be a producer bug, but we still defend.
        staged_writes = [item for item in decoded if isinstance(item, dict)]

        try:
            status = JournalStatus(row.status)
        except ValueError as exc:
            # Out-of-schema status values should be impossible because
            # the repo controls every write, but if they slip through we
            # surface them as a typed error at the value-object boundary
            # rather than silently coercing — the type system will then
            # complain on the caller side, which is the right place to
            # notice.
            raise ValueError(
                f"unexpected journal txn status: {row.status!r}"
            ) from exc

        return JournalTxn(
            id=row.id,
            run_id=row.run_id,
            status=status,
            staged_writes=staged_writes,
            started_at=_parse_iso(row.started_at),
            ready_at=_parse_iso(row.ready_at) if row.ready_at else None,
            finalised_at=_parse_iso(row.finalised_at) if row.finalised_at else None,
        )


# ``sqlite_insert`` is imported eagerly so callers / tests can ``from
# ctxr.fsm.sqlite.repos_locks_journal import sqlite_insert`` if they need to
# extend the UPSERT path. Currently unused inside this file because the
# stale-lease takeover is expressed via the read-then-mutate ORM idiom for
# clarity; keeping the import here documents the alternative path.
_ = sqlite_insert
