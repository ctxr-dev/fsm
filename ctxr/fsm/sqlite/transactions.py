"""Atomic transaction helpers for ctxr.fsm SQLite persistence (W2 substrate).

This module is the single chokepoint through which every state-mutating
unit-of-work in the SQLite substrate must pass. Two flavours are exposed:

* :func:`atomic` — a decorator for callables whose first positional
  parameter is a :class:`sqlalchemy.orm.Session`. It wraps the call in a
  ``BEGIN IMMEDIATE`` block, threads a journal-txn row through the
  lifecycle, and releases / finalises / refuses based on outcome.
* :class:`TransactionContext` — a context manager for callers that prefer
  the imperative shape (``with TransactionContext(engine, run_id) as ctx:
  ctx.session.add(...); ctx.staged_writes.append(...)``).

Both flavours implement the same contract so the engine can mix-and-match
without surprise.

Lifecycle (the "atomic" envelope)
---------------------------------

For each unit-of-work scoped to a ``run_id``:

1. **Refusal check.** :meth:`JournalRepo.inspect` is queried first. If a
   ``pending`` or ``ready_to_finalise`` row exists for the run, we raise
   :class:`JournalRefusedError` — the run is in an unrecovered state and
   the operator must invoke the W3 CLI's ``run resume --journal
   {discard,replay}`` to clear it before new work can start.
2. **Open journal.** :meth:`JournalRepo.open` inserts a fresh ``pending``
   row in its own short transaction (so the row is visible to a recovery
   reader even if the main txn later aborts).
3. **BEGIN IMMEDIATE.** A new ``Session`` is opened and an immediate
   write-lock acquired (``BEGIN IMMEDIATE`` in SQLite). This is what
   serialises writers across processes.
4. **Caller body runs.** The wrapped callable executes with ``session`` as
   its first arg. It may append to ``staged_writes`` (decorator captures
   via a TLS-style holder; context-manager exposes ``ctx.staged_writes``).
5. **On success:**

   a. ``JournalRepo.mark_ready(staged_writes=...)`` inside the same txn,
      so the staged-writes record is part of the atomic flush.
   b. ``session.commit()`` flushes everything to disk.
   c. ``JournalRepo.finalise()`` runs in a *separate* short txn so the
      finalise marker survives even if step (b) is observed atomically.

6. **On exception:** ``session.rollback()`` is called and the JournalTxn
   row is *left* in ``pending`` (or whatever interim status it had
   reached). The W3 CLI / W12 recovery path is responsible for resolving
   it later — we deliberately do NOT auto-discard here because doing so
   would erase the audit trail of the failed attempt.

Why journal-open is in its own short txn
----------------------------------------

If we opened the journal row inside the main ``BEGIN IMMEDIATE`` and the
main txn aborted, the journal row would also disappear — leaving no trace
of the attempted work. That defeats the entire purpose of the journal.
The two-txn shape (open journal → main work → finalise journal) is the
classical write-ahead-log discipline applied at the application level.

Why no engine cache
-------------------

The ``@atomic`` decorator looks up the active sessionmaker through a
:class:`contextvars.ContextVar`. The composition root (an MCP server, a
CLI command, a test fixture) is expected to call
:func:`set_active_session_factory` before invoking any decorated function.
This keeps the substrate free of global engine state and makes parallel
test runs trivial.

Pure ``ctxr.fsm.sqlite`` — no FastAPI, no MCP, no engine code.
"""

from __future__ import annotations

import contextvars
import functools
import inspect
from collections.abc import Callable
from typing import Any, Literal, ParamSpec, TypeVar

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from ctxr.fsm.sqlite.repos_core import get_session_factory
from ctxr.fsm.sqlite.repos_locks_journal import JournalRepo, JournalTxn

__all__ = [
    "AtomicError",
    "JournalRefusedError",
    "TransactionContext",
    "atomic",
    "get_active_session_factory",
    "set_active_session_factory",
]


P = ParamSpec("P")
T = TypeVar("T")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AtomicError(RuntimeError):
    """Base class for atomic-envelope failures.

    Anything raised by :func:`atomic` / :class:`TransactionContext` outside
    the wrapped user code itself inherits from this class so callers can
    catch the substrate-level failure surface in one ``except`` clause and
    let unrelated exceptions propagate.
    """


class JournalRefusedError(AtomicError):
    """A prior journal txn for this run is unrecovered.

    Raised at the very start of :func:`atomic` / ``TransactionContext.__enter__``
    when :meth:`JournalRepo.inspect` returns a row whose status is
    ``pending`` or ``ready_to_finalise``. The caller must invoke the W3
    CLI's ``run resume --journal {discard,replay}`` to resolve the
    outstanding txn before new work can begin.

    The blocking ``JournalTxn`` is attached on :attr:`txn` for diagnostic
    rendering at the CLI / MCP boundary.
    """

    def __init__(self, run_id: str, txn: JournalTxn) -> None:
        super().__init__(
            f"refusing to start atomic txn for run {run_id!r}: "
            f"an outstanding journal txn (id={txn.id!r}, status={txn.status!r}) "
            "exists and must be resolved via "
            "`fsm run resume --journal {discard,replay}` first."
        )
        self.run_id = run_id
        self.txn = txn


# ---------------------------------------------------------------------------
# Active-session-factory context var
# ---------------------------------------------------------------------------


# The composition root binds a sessionmaker via
# :func:`set_active_session_factory`; the decorator picks it up at call
# time. We default to ``None`` so an accidental call from an unwired
# context raises a clear error rather than silently using a stale engine.
_active_session_factory: contextvars.ContextVar[sessionmaker[Session] | None] = (
    contextvars.ContextVar("ctxr_fsm_active_session_factory", default=None)
)


def set_active_session_factory(
    factory: sessionmaker[Session] | None,
) -> contextvars.Token[sessionmaker[Session] | None]:
    """Bind a sessionmaker for the current context.

    Returns the ``Token`` so callers can later call
    ``_active_session_factory.reset(token)`` to restore the previous
    binding — classical ``contextvars`` discipline. Tests use this to
    swap factories per-case without leaking state.
    """
    return _active_session_factory.set(factory)


def get_active_session_factory() -> sessionmaker[Session]:
    """Return the active sessionmaker.

    Raises :class:`AtomicError` if none is bound — that almost always
    means the composition root forgot to call
    :func:`set_active_session_factory` at startup.
    """
    factory = _active_session_factory.get()
    if factory is None:
        raise AtomicError(
            "no active sessionmaker is bound. Call "
            "`set_active_session_factory(get_session_factory(engine))` "
            "in your composition root before invoking @atomic functions."
        )
    return factory


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _begin_immediate(session: Session) -> None:
    """Promote the session's implicit txn to ``BEGIN IMMEDIATE``.

    SQLite's default transaction is a "deferred" one that takes the
    write-lock lazily on the first write. ``BEGIN IMMEDIATE`` instead
    acquires the write-lock up front, which is what we want for the
    engine's serialised-writer model: any concurrent attempt to start
    another immediate txn fails fast with ``database is locked`` rather
    than racing for the lock at first-write time.

    Issued via the DB-API connection directly because SQLAlchemy's own
    ``session.begin()`` machinery has already started an implicit txn by
    the time we get here; we have to roll that back first and then issue
    our own ``BEGIN IMMEDIATE``.
    """
    # Close any implicit transaction SQLAlchemy may have opened so we can
    # start our own under our own terms.
    session.rollback()
    session.execute(text("BEGIN IMMEDIATE"))


def _make_journal_session(engine: Engine) -> Session:
    """Open a short-lived session for journal-open / journal-finalise.

    These run in their own transactions (see the module docstring) so
    they do not piggyback on the caller's session lifecycle.
    """
    return Session(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def _refuse_if_outstanding(engine: Engine, run_id: str) -> None:
    """Raise :class:`JournalRefusedError` if a non-finalised txn exists."""
    repo = JournalRepo()
    with _make_journal_session(engine) as scout:
        existing = repo.inspect(scout, run_id=run_id)
    if existing is not None and existing.status in ("pending", "ready_to_finalise"):
        raise JournalRefusedError(run_id=run_id, txn=existing)


def _open_journal_row(engine: Engine, run_id: str) -> JournalTxn:
    """Insert a fresh ``pending`` journal row in its own short txn."""
    repo = JournalRepo()
    with _make_journal_session(engine) as opener, opener.begin():
        txn = repo.open(opener, run_id=run_id)
    return txn


def _finalise_journal_row(engine: Engine, txn_id: str) -> JournalTxn:
    """Flip a ``ready_to_finalise`` row to ``finalised`` in its own txn."""
    repo = JournalRepo()
    with _make_journal_session(engine) as closer, closer.begin():
        txn = repo.finalise(closer, txn_id=txn_id)
    return txn


def _resolve_engine_and_factory(
    engine: Engine | None,
) -> tuple[Engine, sessionmaker[Session]]:
    """Return the (engine, sessionmaker) pair to use for this txn.

    If ``engine`` is explicit, derive a sessionmaker on the fly via
    :func:`get_session_factory`. Otherwise pull the active sessionmaker
    from the context var and extract its bound engine.
    """
    if engine is not None:
        return engine, get_session_factory(engine)
    factory = get_active_session_factory()
    bound = factory.kw.get("bind")
    if not isinstance(bound, Engine):
        raise AtomicError(
            "active sessionmaker has no Engine bound; "
            "rebind via `set_active_session_factory(get_session_factory(engine))`."
        )
    return bound, factory


# ---------------------------------------------------------------------------
# TransactionContext
# ---------------------------------------------------------------------------


class TransactionContext:
    """Imperative context-manager flavour of the atomic envelope.

    Usage::

        with TransactionContext(engine, run_id="...") as ctx:
            ctx.session.add(SomeRow(...))
            ctx.staged_writes.append({"path": "manifest.json"})

    On clean exit:

    * ``JournalRepo.mark_ready`` is called with the accumulated
      ``staged_writes`` inside the same transaction;
    * the session commits;
    * ``JournalRepo.finalise`` runs in a separate short txn.

    On exception:

    * the session is rolled back; the journal row stays ``pending``
      (or whatever interim status it had reached) for W3 recovery.

    ``staged_writes`` is a plain list[dict[str, Any]] — callers may
    append rows of any shape they want recorded against the txn. The
    canonical shape is ``{"path": "...", "hash": "..."}`` but the
    journal column is opaque JSON so any serialisable dict is fine.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        run_id: str,
    ) -> None:
        self._engine = engine
        self.run_id = run_id
        self._session: Session | None = None
        self._journal_txn: JournalTxn | None = None
        self._journal_repo = JournalRepo()
        #: Mutable list the caller appends staged-write records to.
        self.staged_writes: list[dict[str, Any]] = []

    @property
    def session(self) -> Session:
        """The active session — only valid between ``__enter__`` and exit."""
        if self._session is None:
            raise AtomicError(
                "TransactionContext.session accessed outside the with-block."
            )
        return self._session

    @property
    def journal_txn(self) -> JournalTxn:
        """The journal-txn row covering this transaction."""
        if self._journal_txn is None:
            raise AtomicError(
                "TransactionContext.journal_txn accessed outside the with-block."
            )
        return self._journal_txn

    def __enter__(self) -> TransactionContext:
        # Step 1: refuse if there's an outstanding txn for this run.
        _refuse_if_outstanding(self._engine, self.run_id)
        # Step 2: open the journal row in its own short txn so it
        # survives main-txn abort.
        self._journal_txn = _open_journal_row(self._engine, self.run_id)
        # Step 3: open the main session and take the write-lock up front.
        factory = get_session_factory(self._engine)
        session = factory()
        try:
            _begin_immediate(session)
        except Exception:
            session.close()
            raise
        self._session = session
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any,
    ) -> Literal[False]:
        session = self._session
        txn = self._journal_txn
        # Defensive: __enter__ wires both fields. If we get here without
        # them, something is very wrong — but we must not raise from
        # __exit__ if there's already an active exception, so we just
        # let the existing exception propagate.
        if session is None or txn is None:
            return False

        try:
            if exc is None:
                # Happy path: mark_ready inside the live txn, then commit,
                # then finalise in a separate short txn.
                self._journal_repo.mark_ready(
                    session,
                    txn_id=txn.id,
                    staged_writes=list(self.staged_writes),
                )
                session.commit()
                self._journal_txn = _finalise_journal_row(self._engine, txn.id)
            else:
                # Abort path: rollback the main session; leave the
                # journal row in 'pending' for W3 recovery to inspect.
                session.rollback()
        finally:
            session.close()
            self._session = None

        # Returning False propagates any user exception; we never swallow.
        return False


# ---------------------------------------------------------------------------
# @atomic decorator
# ---------------------------------------------------------------------------


def atomic[**P, T](fn: Callable[P, T]) -> Callable[P, T]:
    """Wrap ``fn`` so it runs inside the atomic-tx envelope.

    Contract:

    * The wrapped callable's *first parameter* must be a
      :class:`sqlalchemy.orm.Session`. We assert this via
      :mod:`inspect.signature` at decoration time so misuse is caught
      at import, not at call time.
    * The wrapped callable's *second parameter* (or keyword) must be
      ``run_id`` — the journal key. Either positional or keyword is
      accepted at call time.
    * The keyword ``staged_writes=[...]`` may be supplied by the caller
      to override the empty default. If present, it is **consumed** by
      the decorator (popped from kwargs before calling ``fn``) so the
      wrapped function does not have to accept it.
    * An optional keyword ``engine=<Engine>`` may be supplied to
      explicitly bind the txn to a specific engine; otherwise the
      active context-var sessionmaker is used.

    The session passed into ``fn`` is owned by the decorator: ``fn``
    must never call ``session.commit()`` or ``session.rollback()``
    itself; doing so would unsync the journal row from the data.
    """
    sig = inspect.signature(fn)
    params = list(sig.parameters.values())
    if not params:
        raise AtomicError(
            f"@atomic-wrapped function {fn.__qualname__!r} must take a "
            "Session as its first parameter (got no parameters at all)."
        )
    # The first parameter must accept a Session positionally. We do not
    # check the annotation strictly (it might be `Session`, `"Session"`,
    # or an alias), but we *do* refuse a keyword-only first parameter
    # because we always pass session positionally.
    first = params[0]
    if first.kind not in (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    ):
        raise AtomicError(
            f"@atomic-wrapped function {fn.__qualname__!r}: first parameter "
            f"{first.name!r} must be positional (got kind={first.kind!r})."
        )

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> T:
        # Pull our control kwargs out before they reach `fn`.
        engine: Engine | None = kwargs.pop("engine", None)
        staged_writes: list[dict[str, Any]] = list(
            kwargs.pop("staged_writes", []) or []
        )

        run_id = _extract_run_id(fn, args, kwargs)

        bound_engine, factory = _resolve_engine_and_factory(engine)

        # Step 1: refusal check.
        _refuse_if_outstanding(bound_engine, run_id)

        # Step 2: open journal row in its own short txn.
        journal_repo = JournalRepo()
        journal_txn = _open_journal_row(bound_engine, run_id)

        # Step 3: open main session, take BEGIN IMMEDIATE write-lock.
        session = factory()
        try:
            _begin_immediate(session)
        except Exception:
            session.close()
            raise

        try:
            # Step 4: run the caller body with `session` as first arg.
            result = fn(session, *args, **kwargs)  # type: ignore[arg-type]
        except Exception:
            # Abort path: rollback, leave journal row in 'pending'.
            try:
                session.rollback()
            finally:
                session.close()
            raise

        # Step 5a: mark_ready inside the same txn so the staged-writes
        # snapshot is atomic with the data flush.
        try:
            journal_repo.mark_ready(
                session,
                txn_id=journal_txn.id,
                staged_writes=staged_writes,
            )
            # Step 5b: commit the main txn.
            session.commit()
        except Exception:
            try:
                session.rollback()
            finally:
                session.close()
            raise
        finally:
            # Session is single-use; close as soon as we are done with it.
            if session.is_active:
                session.close()
            else:
                session.close()

        # Step 5c: finalise the journal row in a separate short txn so
        # its closed-marker is committed even if downstream code fails.
        _finalise_journal_row(bound_engine, journal_txn.id)

        return result

    # Attach the resolved signature for introspection (e.g. tests / docs).
    wrapper.__wrapped__ = fn
    return wrapper


def _extract_run_id(
    fn: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> str:
    """Pull ``run_id`` from the wrapped call's args/kwargs.

    ``run_id`` may be passed positionally (it is the *first* user-visible
    parameter — the Session slot is filled by the decorator, not by the
    caller) or as a keyword. We bind against the signature minus the
    Session parameter so the positional indexing is intuitive at the
    call site.
    """
    if "run_id" in kwargs:
        run_id = kwargs["run_id"]
    elif args:
        # First positional arg from the caller's POV is the parameter
        # immediately after `session` in the wrapped function's signature.
        sig = inspect.signature(fn)
        params = list(sig.parameters.values())[1:]  # drop session
        # Find the index of run_id in the remaining params.
        try:
            idx = next(i for i, p in enumerate(params) if p.name == "run_id")
        except StopIteration as exc:
            raise AtomicError(
                f"@atomic-wrapped function {fn.__qualname__!r} must accept a "
                "`run_id` parameter (positional or keyword)."
            ) from exc
        if idx >= len(args):
            raise AtomicError(
                f"@atomic call to {fn.__qualname__!r}: `run_id` not supplied."
            )
        run_id = args[idx]
    else:
        raise AtomicError(
            f"@atomic call to {fn.__qualname__!r}: `run_id` not supplied."
        )

    if not isinstance(run_id, str):
        raise AtomicError(
            f"@atomic call to {fn.__qualname__!r}: `run_id` must be a str "
            f"(got {type(run_id).__name__})."
        )
    return run_id
