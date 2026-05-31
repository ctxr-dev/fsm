"""Process-local registry for inline state handlers.

An inline state is a fourth FSM state kind (alongside ``worker``,
``loop``, and ``terminal``) declared via
:class:`~ctxr.fsm.core.models.InlineSpec`. Unlike worker states, an
inline state's outputs are produced by a deterministic Python callable
that runs in-process during :func:`~ctxr.fsm.core.engine.execute_inline`
inside the same atomic transaction as the surrounding state
transition.  This module owns the lookup table from
``(spec_id, handler_id)`` to that callable.

The registry is deliberately tiny and dependency-free:

* **Process-local.** A single dict on a single object instance. No
  global mutable state beyond the module-level default registry
  exposed via :func:`get_default_registry`, and no per-process
  locks (the engine layer is single-writer per-run via the W2 SQLite
  locks; concurrent reads of a fully-populated registry are safe by
  CPython's GIL-backed dict read semantics).
* **No I/O.** Registration is a pure mapping update; lookup is a pure
  mapping read.  The consumer's lifecycle (typically: register all
  handlers at module-import time, then run engine work that calls
  ``lookup``) is the same shape pickle-safe registries use.
* **Per-spec scoping.** The registry keys on the pair
  ``(spec_id, handler_id)``, so two different specs can reuse the
  same ``handler_id`` slug without collision.

The shape mirrors the W12 verifier handler pattern in
:mod:`ctxr.fsm.core.verifier` but with one knob more: per-spec
scoping. Where the verifier has at most one registered handler per
process, an inline registry can hold arbitrarily many keyed pairs so
multiple FSM specs can be active in the same Python process (the
common case in a long-running supervisor / MCP server).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "InlineContext",
    "InlineHandler",
    "InlineHandlerRegistry",
    "discover_handlers_via_entry_points",
    "get_default_registry",
]


# ---------------------------------------------------------------------------
# InlineContext — read-only handler input envelope
# ---------------------------------------------------------------------------


class InlineContext(BaseModel):
    """Read-only context handed to an inline handler at advance time.

    The shape mirrors the data already available inside the engine
    when it resolves a state's body: the owning run, the state being
    executed, the loop iteration counter (for inline states embedded
    in loop bodies, if/when that becomes a thing), the run's startup
    ``args``, and the resolved ``inputs`` dict (per the surrounding
    :class:`~ctxr.fsm.core.models.State.outputs` declaration, materialised
    from prior-state outputs by the engine).

    Fields are immutable so a handler cannot accidentally mutate the
    context and influence subsequent handler calls in the same
    process. ``arbitrary_types_allowed=True`` is set because
    :class:`uuid.UUID` is not a Pydantic-native type but Pydantic v2
    serialises it cleanly when explicitly allowed.
    """

    model_config = ConfigDict(
        strict=True,
        frozen=True,
        extra="forbid",
        arbitrary_types_allowed=True,
    )

    run_id: uuid.UUID
    fsm_id: str
    state_id: str
    iteration_n: int | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    inputs: dict[str, Any] = Field(default_factory=dict)


InlineHandler = Callable[[InlineContext], dict[str, Any]]
"""Signature of a registered inline state handler.

The handler receives a fully-populated :class:`InlineContext` and MUST
return a plain ``dict[str, Any]`` carrying the state's outputs. The
returned dict is validated by the engine against the inline state's
:attr:`~ctxr.fsm.core.models.InlineSpec.response_schema` (when one is
declared) and any
:attr:`~ctxr.fsm.core.models.InlineSpec.post_validations` predicates
before the engine advances. Handlers MUST be deterministic and free
of I/O — they run inside the engine's atomic transaction; raising or
hanging here pauses the whole transition.
"""


# ---------------------------------------------------------------------------
# InlineHandlerRegistry — process-local lookup table
# ---------------------------------------------------------------------------


class InlineHandlerRegistry:
    """A per-process registry of inline handlers keyed by ``(spec_id, handler_id)``.

    The registry is a thin wrapper around a single dict; the wrapper
    exists for ergonomic methods and a stable type to import. No
    locking is performed: the typical pattern is "register at import,
    look up during runs" and CPython's GIL makes individual dict reads
    and writes atomic. Concurrent ``register`` and ``lookup`` from
    multiple threads is therefore safe, though a handler observed via
    ``lookup`` immediately before a concurrent ``unregister`` may be
    invoked anyway — the engine is single-writer per-run so the
    interleaving cannot happen in normal use.
    """

    __slots__ = ("_handlers",)

    def __init__(self) -> None:
        self._handlers: dict[tuple[str, str], InlineHandler] = {}

    def register(
        self,
        spec_id: str,
        handler_id: str,
        handler: InlineHandler,
    ) -> None:
        """Register ``handler`` against ``(spec_id, handler_id)``.

        Re-registering the same key overwrites the previous handler.
        Empty / non-string keys raise ``ValueError`` immediately so a
        bad call site fails loudly rather than poisoning a downstream
        lookup.
        """
        if not isinstance(spec_id, str) or not spec_id:
            raise ValueError("spec_id must be a non-empty string")
        if not isinstance(handler_id, str) or not handler_id:
            raise ValueError("handler_id must be a non-empty string")
        if not callable(handler):
            raise ValueError("handler must be callable")
        self._handlers[(spec_id, handler_id)] = handler

    def register_many(
        self,
        spec_id: str,
        handlers: dict[str, InlineHandler],
    ) -> None:
        """Bulk-register every ``handler_id -> callable`` entry under ``spec_id``.

        Convenience for the common skill bootstrap pattern of declaring
        an ``INLINE_HANDLERS`` module-level dict and registering the
        whole batch at startup. Each pair is delegated to
        :meth:`register`, so the same validation applies.
        """
        for handler_id, handler in handlers.items():
            self.register(spec_id, handler_id, handler)

    def lookup(
        self,
        spec_id: str,
        handler_id: str,
    ) -> InlineHandler | None:
        """Return the registered handler for ``(spec_id, handler_id)``, or ``None``.

        Returning ``None`` for an unknown key is intentional — the
        engine layer wraps the missing handler in a structured
        :class:`~ctxr.fsm.core.models.InlineExecutionResult` so the
        fault is reported, not raised. This keeps the engine's failure
        handling uniform across "handler missing" and "handler raised"
        cases.
        """
        return self._handlers.get((spec_id, handler_id))

    def unregister(
        self,
        spec_id: str,
        handler_id: str | None = None,
    ) -> None:
        """Remove registrations for ``spec_id``.

        When ``handler_id`` is ``None``, drop every handler registered
        under ``spec_id``. Otherwise drop only the single
        ``(spec_id, handler_id)`` entry. Missing entries are a no-op
        so the operation is idempotent.
        """
        if handler_id is None:
            self._handlers = {
                key: value
                for key, value in self._handlers.items()
                if key[0] != spec_id
            }
            return
        self._handlers.pop((spec_id, handler_id), None)

    def clear(self) -> None:
        """Remove all registrations.

        Useful in test setUp/tearDown to keep registries hermetic
        between tests. The default module-singleton registry retains
        its identity across calls; only its contents are wiped.
        """
        self._handlers.clear()


# ---------------------------------------------------------------------------
# Module-level default registry
# ---------------------------------------------------------------------------


_default_registry: InlineHandlerRegistry = InlineHandlerRegistry()


def get_default_registry() -> InlineHandlerRegistry:
    """Return the process-wide default :class:`InlineHandlerRegistry`.

    Higher-level callers (the engine, the SQLite repository layer that
    drives ``engine.execute_inline``) accept an optional ``registry``
    argument so tests can pass a hermetic instance; production code
    falls back to this singleton. The singleton is created at import
    time so importing this module is enough to make it available.
    """
    return _default_registry


# ---------------------------------------------------------------------------
# Cross-process entry-points-based handler discovery
# ---------------------------------------------------------------------------
#
# Skills register inline handlers in their OWN Python process (typically
# via ``python -m <skill>.install`` or some equivalent bootstrap). The
# MCP server / FastAPI server / supervisor run in DIFFERENT processes.
# Those server processes' default registries start empty — they have no
# way to know what handlers a skill installed in some other process.
#
# Resolution: skills declare an entry point in their pyproject.toml:
#
#     [project.entry-points."ctxr_fsm.skills"]
#     skill-code-review = "ctxr_skill_code_review.install:register"
#
# Server processes (or anyone needing a handler that's not present in
# their local registry) call :func:`discover_handlers_via_entry_points`
# to walk every registered entry-point and invoke its ``register()``
# function. Each skill's ``register()`` is idempotent and re-registers
# its INLINE_HANDLERS into whatever registry it can reach (typically
# the default singleton via :func:`get_default_registry`), so the
# server's registry gets populated in-place.

_EP_GROUP: str = "ctxr_fsm.skills"
"""Entry-points group every ctxr-fsm-driven skill declares in its
pyproject.toml. Pinned here as the single source of truth so adding a
new server-side caller is a one-line ``from inline_registry import
_EP_GROUP`` away from speaking the same convention."""


_discovery_cache: set[str] = set()
"""Names of entry points whose ``register()`` has already been called in
THIS process. Lazy discovery is the dominant pattern: the first time
the MCP server's `commit_outputs` advances to an inline state and finds
the registry empty, it calls ``discover_handlers_via_entry_points()``,
which short-circuits subsequent calls so the bootstrap cost is paid
once per process lifetime."""


def discover_handlers_via_entry_points(
    *,
    force: bool = False,
) -> dict[str, str | None]:
    """Walk ``ctxr_fsm.skills`` entry points and invoke each ``register()``.

    Eager bulk discovery: every entry point registered against the
    ``ctxr_fsm.skills`` group is loaded and its callable invoked,
    which is expected to populate the default
    :class:`InlineHandlerRegistry` with that skill's handlers + the
    spec row in the local project DB.

    Used by server processes (MCP / FastAPI) when their local registry
    misses a handler lookup: the missing handler is almost always a
    cross-process situation (the skill registered in process A, the
    server runs in process B), and entry-points are the standard
    cross-process discovery mechanism.

    Parameters
    ----------
    force:
        When ``False`` (default), entry points already invoked in this
        process are skipped. When ``True``, every entry point is
        re-loaded + re-invoked (useful for tests that monkeypatch the
        entry-points and want a fresh walk).

    Returns
    -------
    dict[str, str | None]
        Map of ``entry_point_name -> error_message``. ``error_message``
        is ``None`` on success. Any exception raised by an entry-point's
        ``register()`` is captured into the value so a single broken
        skill does not poison discovery for the rest of the workspace.
    """

    # Lazy import: ``importlib.metadata`` is stdlib (>= 3.10) so the
    # import is cheap, but we keep it local so the registry module's
    # top-level remains zero-dependency for unit tests that monkeypatch.
    from importlib.metadata import entry_points

    outcomes: dict[str, str | None] = {}
    # ``entry_points(group=...)`` in 3.12 returns an EntryPoints
    # collection that supports iteration. Older Python releases (pre-3.10)
    # would need the fallback shim; this package's pyproject pins
    # ``requires-python >= 3.12`` so we use the modern surface directly.
    eps = entry_points(group=_EP_GROUP)
    for ep in eps:
        if not force and ep.name in _discovery_cache:
            outcomes[ep.name] = None  # already loaded successfully
            continue
        try:
            register_fn = ep.load()
            register_fn()
        except Exception as exc:  # one bad skill must not poison discovery for the rest
            outcomes[ep.name] = f"{type(exc).__name__}: {exc}"
        else:
            outcomes[ep.name] = None
            _discovery_cache.add(ep.name)
    return outcomes


def lookup_with_discovery(
    spec_id: str,
    handler_id: str,
    *,
    registry: InlineHandlerRegistry | None = None,
) -> InlineHandler | None:
    """Look up a handler with one round of cross-process discovery on miss.

    Convenience for server processes that drive inline-state advancement
    without knowing whether the handlers were registered locally or in
    a different process. The flow is:

    1. Look up in the supplied (or default) registry.
    2. If found, return.
    3. Otherwise call :func:`discover_handlers_via_entry_points` once
       (subsequent calls are short-circuited by the discovery cache).
    4. Look up again.
    5. Return whatever we have — possibly still ``None`` if no
       registered skill owns the requested handler.

    Tests that want to assert "lookup never goes through discovery"
    should use :meth:`InlineHandlerRegistry.lookup` directly.
    """

    registry = registry or _default_registry
    handler = registry.lookup(spec_id, handler_id)
    if handler is not None:
        return handler
    discover_handlers_via_entry_points()
    return registry.lookup(spec_id, handler_id)
