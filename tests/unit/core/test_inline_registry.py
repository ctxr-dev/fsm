"""Tests for :mod:`ctxr.fsm.core.inline_registry` (W14a).

Covers:

* :meth:`InlineHandlerRegistry.register` happy path and per-spec scoping.
* :meth:`InlineHandlerRegistry.lookup` for known + unknown keys.
* :meth:`InlineHandlerRegistry.register_many` bulk-registration.
* :meth:`InlineHandlerRegistry.unregister` for one entry + entire spec.
* :meth:`InlineHandlerRegistry.clear` wipes everything.
* :func:`get_default_registry` returns a single process-wide singleton.
* :meth:`InlineHandlerRegistry.register` argument validation.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from ctxr.fsm.core.inline_registry import (
    InlineContext,
    InlineHandlerRegistry,
    get_default_registry,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_handler(value: str) -> Any:
    """Build a handler that returns a payload dict tagged with ``value``."""

    def _handler(ctx: InlineContext) -> dict[str, Any]:
        return {"tag": value, "run_id": str(ctx.run_id)}

    return _handler


def _ctx(fsm_id: str = "spec-a", state_id: str = "s1") -> InlineContext:
    return InlineContext(
        run_id=uuid4(),
        fsm_id=fsm_id,
        state_id=state_id,
    )


# ---------------------------------------------------------------------------
# register / lookup
# ---------------------------------------------------------------------------


def test_register_then_lookup_returns_same_callable() -> None:
    registry = InlineHandlerRegistry()
    handler = _make_handler("h1")
    registry.register("spec-a", "h1", handler)

    found = registry.lookup("spec-a", "h1")
    assert found is handler


def test_lookup_unknown_key_returns_none() -> None:
    registry = InlineHandlerRegistry()
    assert registry.lookup("spec-x", "missing") is None


def test_register_overwrites_existing_key() -> None:
    registry = InlineHandlerRegistry()
    first = _make_handler("first")
    second = _make_handler("second")
    registry.register("spec-a", "h1", first)
    registry.register("spec-a", "h1", second)

    found = registry.lookup("spec-a", "h1")
    assert found is second
    # The first handler is gone.
    out = found(_ctx())  # type: ignore[misc]
    assert out["tag"] == "second"


# ---------------------------------------------------------------------------
# per-spec scoping
# ---------------------------------------------------------------------------


def test_same_handler_id_under_different_specs_does_not_collide() -> None:
    """Two specs can register the same handler_id without overlap."""
    registry = InlineHandlerRegistry()
    handler_a = _make_handler("from-a")
    handler_b = _make_handler("from-b")
    registry.register("spec-a", "collect_findings", handler_a)
    registry.register("spec-b", "collect_findings", handler_b)

    found_a = registry.lookup("spec-a", "collect_findings")
    found_b = registry.lookup("spec-b", "collect_findings")
    assert found_a is handler_a
    assert found_b is handler_b
    assert found_a is not found_b


# ---------------------------------------------------------------------------
# register_many
# ---------------------------------------------------------------------------


def test_register_many_registers_every_pair() -> None:
    registry = InlineHandlerRegistry()
    handlers = {
        "alpha": _make_handler("a"),
        "beta": _make_handler("b"),
        "gamma": _make_handler("g"),
    }
    registry.register_many("spec-a", handlers)

    assert registry.lookup("spec-a", "alpha") is handlers["alpha"]
    assert registry.lookup("spec-a", "beta") is handlers["beta"]
    assert registry.lookup("spec-a", "gamma") is handlers["gamma"]


def test_register_many_empty_dict_is_noop() -> None:
    registry = InlineHandlerRegistry()
    registry.register_many("spec-a", {})
    # No registrations created.
    assert registry.lookup("spec-a", "any") is None


# ---------------------------------------------------------------------------
# unregister
# ---------------------------------------------------------------------------


def test_unregister_single_entry_leaves_other_entries_intact() -> None:
    registry = InlineHandlerRegistry()
    registry.register("spec-a", "h1", _make_handler("h1"))
    registry.register("spec-a", "h2", _make_handler("h2"))
    registry.register("spec-b", "h1", _make_handler("h1-b"))

    registry.unregister("spec-a", "h1")

    assert registry.lookup("spec-a", "h1") is None
    assert registry.lookup("spec-a", "h2") is not None
    assert registry.lookup("spec-b", "h1") is not None


def test_unregister_entire_spec_removes_all_its_handlers() -> None:
    registry = InlineHandlerRegistry()
    registry.register("spec-a", "h1", _make_handler("h1"))
    registry.register("spec-a", "h2", _make_handler("h2"))
    registry.register("spec-b", "h1", _make_handler("h1-b"))

    registry.unregister("spec-a")

    assert registry.lookup("spec-a", "h1") is None
    assert registry.lookup("spec-a", "h2") is None
    assert registry.lookup("spec-b", "h1") is not None


def test_unregister_missing_entry_is_idempotent_noop() -> None:
    registry = InlineHandlerRegistry()
    registry.register("spec-a", "h1", _make_handler("h1"))
    # Should not raise; should leave existing registrations untouched.
    registry.unregister("spec-a", "does_not_exist")
    registry.unregister("nope_spec")
    assert registry.lookup("spec-a", "h1") is not None


# ---------------------------------------------------------------------------
# clear
# ---------------------------------------------------------------------------


def test_clear_removes_all_registrations() -> None:
    registry = InlineHandlerRegistry()
    registry.register("spec-a", "h1", _make_handler("a1"))
    registry.register("spec-b", "h1", _make_handler("b1"))
    registry.clear()

    assert registry.lookup("spec-a", "h1") is None
    assert registry.lookup("spec-b", "h1") is None


# ---------------------------------------------------------------------------
# default registry singleton identity
# ---------------------------------------------------------------------------


def test_get_default_registry_returns_singleton() -> None:
    a = get_default_registry()
    b = get_default_registry()
    assert a is b


def test_default_registry_is_an_inline_handler_registry() -> None:
    r = get_default_registry()
    assert isinstance(r, InlineHandlerRegistry)


def test_default_registry_state_is_preserved_across_calls() -> None:
    r = get_default_registry()
    try:
        r.register("test-spec-default-singleton", "h1", _make_handler("x"))
        # Re-fetch and ensure the same registrations persist.
        again = get_default_registry()
        assert again.lookup("test-spec-default-singleton", "h1") is not None
    finally:
        # Keep tests hermetic for any later default-registry tests.
        r.unregister("test-spec-default-singleton")


# ---------------------------------------------------------------------------
# argument validation on register
# ---------------------------------------------------------------------------


def test_register_rejects_empty_spec_id() -> None:
    registry = InlineHandlerRegistry()
    with pytest.raises(ValueError):
        registry.register("", "h1", _make_handler("x"))


def test_register_rejects_empty_handler_id() -> None:
    registry = InlineHandlerRegistry()
    with pytest.raises(ValueError):
        registry.register("spec-a", "", _make_handler("x"))


def test_register_rejects_non_callable_handler() -> None:
    registry = InlineHandlerRegistry()
    with pytest.raises(ValueError):
        registry.register("spec-a", "h1", "not_callable")  # type: ignore[arg-type]
