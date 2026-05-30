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
