"""Unit test for the Principle 0 ("Bootstrap before work") section.

Principle 0 is the entry-point gate every FSM-using skill MUST pass
through before reaching Principles 1 through 10. It lives at the very top of
``ctxr/fsm/memory/principles.md`` (immediately under the introductory
paragraph, ahead of "Principle 1: pre-check before you act") and
points the reader at ``bootstrap.md`` for the procedure rather than
inlining the steps.

The test pins three invariants the rest of the W14 pipeline depends on:

* The Principle 0 heading is present in the canonical file.
* It appears BEFORE Principle 1 (ordering matters — bootstrap is the
  precondition, the rest are the runtime contract).
* It references ``@.ctxr-fsm/memory/bootstrap.md`` so the in-project
  staged copy of the bootstrap doc is the resolution target.
"""

from __future__ import annotations

from ctxr.fsm.memory import get_principles_path


def test_principles_contains_principle_0_heading() -> None:
    """``Principle 0`` appears verbatim as a section heading."""

    text = get_principles_path("canonical").read_text(encoding="utf-8")
    assert "## Principle 0: Bootstrap before work" in text


def test_principle_0_precedes_principle_1() -> None:
    """Principle 0 sits above Principle 1 in the document."""

    text = get_principles_path("canonical").read_text(encoding="utf-8")
    p0 = text.index("## Principle 0:")
    p1 = text.index("## Principle 1:")
    assert p0 < p1, "Principle 0 must appear before Principle 1"


def test_principle_0_references_bootstrap_doc() -> None:
    """Principle 0's body points at ``@.ctxr-fsm/memory/bootstrap.md``."""

    text = get_principles_path("canonical").read_text(encoding="utf-8")
    # Slice from Principle 0's heading to the next ``## Principle`` so
    # the assertion targets ONLY Principle 0's prose (not an accidental
    # mention further down the file).
    start = text.index("## Principle 0:")
    end = text.index("## Principle 1:", start)
    principle_0 = text[start:end]
    assert "@.ctxr-fsm/memory/bootstrap.md" in principle_0


def test_principle_0_present_in_every_client_adapter() -> None:
    """Each per-client adapter inherits Principle 0 from the canonical file."""

    for client in ("claude", "codex", "cursor"):
        body = get_principles_path(client).read_text(encoding="utf-8")
        assert "## Principle 0: Bootstrap before work" in body, client
        assert "@.ctxr-fsm/memory/bootstrap.md" in body, client
