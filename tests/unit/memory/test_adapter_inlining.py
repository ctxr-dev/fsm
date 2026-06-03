"""Unit tests for ``tools/generate_memory_adapters.py``'s inlining contract.

W14e contract: Claude's principles adapter keeps the canonical
``@.ctxr-fsm/memory/bootstrap.md`` import unchanged (Claude follows
the import natively). Codex and Cursor's adapters INLINE the
bootstrap body inside Principle 0 — fenced by HTML markers
(``bootstrap-content-begin`` / ``bootstrap-content-end``) so the
content is reachable to LLMs that don't follow ``@`` imports — and
demote every bootstrap heading by two levels so the nested block
reads cleanly under Principle 0 (H2).

These tests exercise the generator directly (no install-memory CLI
hop) so failures point at the generator's contract, not at a
downstream consumer.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT: Path = Path(__file__).resolve().parents[3]
GENERATOR_PATH: Path = REPO_ROOT / "tools" / "generate_memory_adapters.py"
_MODULE_NAME: str = "generate_memory_adapters"


def _load_generator_module() -> object:
    """Import ``tools/generate_memory_adapters.py`` as a module.

    The generator lives outside any installed package, so we load it
    from its file path via :mod:`importlib.util`. We must register
    the module in :data:`sys.modules` BEFORE calling
    ``exec_module`` because the script defines a ``@dataclass``,
    and Python 3.13's dataclass internals do a ``sys.modules.get
    (cls.__module__)`` lookup that would otherwise return ``None``
    and crash with ``AttributeError: 'NoneType' object has no
    attribute '__dict__'``.
    """

    spec = importlib.util.spec_from_file_location(
        _MODULE_NAME, GENERATOR_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        # On import failure, evict the partially-loaded module so a
        # follow-up test doesn't pick up a half-initialised stub.
        sys.modules.pop(_MODULE_NAME, None)
        raise
    return module


@pytest.fixture
def gen_mod() -> object:
    """Fresh import of the generator script for each test."""

    return _load_generator_module()


# ---------------------------------------------------------------------------
# Heading demotion
# ---------------------------------------------------------------------------


def test_demote_headings_zero_is_identity(gen_mod: object) -> None:
    """``_demote_headings(text, 0)`` returns the input byte-for-byte."""

    text = "# H1\n\n## H2\n\nbody\n### H3\n"
    assert gen_mod._demote_headings(text, 0) == text  # type: ignore[attr-defined]


def test_demote_headings_increases_by_levels(gen_mod: object) -> None:
    """Each heading's leading ``#`` count increases by ``levels``."""

    text = "# H1\n## H2\n### H3\n"
    expected = "### H1\n#### H2\n##### H3\n"
    assert gen_mod._demote_headings(text, 2) == expected  # type: ignore[attr-defined]


def test_demote_headings_caps_at_h6(gen_mod: object) -> None:
    """Headings cannot exceed H6 — HTML has no H7."""

    text = "##### H5\n"  # H5 + 2 should land at H6, not H7
    assert gen_mod._demote_headings(text, 2) == "###### H5\n"  # type: ignore[attr-defined]


def test_demote_headings_h6_passthrough(gen_mod: object) -> None:
    """H6 headings are not matched (already at the cap) and pass through."""

    text = "###### H6\n"
    assert gen_mod._demote_headings(text, 2) == text  # type: ignore[attr-defined]


def test_demote_headings_ignores_in_paragraph_hashes(gen_mod: object) -> None:
    """``#`` characters mid-paragraph are not headings and are untouched."""

    text = "## heading\n\nThis is #not a heading.\n"
    expected = "#### heading\n\nThis is #not a heading.\n"
    assert gen_mod._demote_headings(text, 2) == expected  # type: ignore[attr-defined]


def test_demote_headings_rejects_negative(gen_mod: object) -> None:
    """Promoting (negative demote) is not supported."""

    with pytest.raises(ValueError):
        gen_mod._demote_headings("# H1\n", -1)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Generated adapter contract
# ---------------------------------------------------------------------------


def _render_all(gen_mod: object) -> dict[str, str]:
    """Render every adapter against the live canonical principles.

    Returns a ``{filename: rendered_text}`` mapping so individual tests
    can pick the adapter they need without re-rendering.
    """

    canonical_text = gen_mod.CANONICAL.read_text(encoding="utf-8")  # type: ignore[attr-defined]
    return {
        adapter.filename: gen_mod._render(canonical_text, adapter)  # type: ignore[attr-defined]
        for adapter in gen_mod.ADAPTERS  # type: ignore[attr-defined]
    }


def test_claude_adapter_keeps_at_import_unchanged(gen_mod: object) -> None:
    """Claude's adapter does NOT inline the bootstrap; the ``@`` import stays."""

    rendered = _render_all(gen_mod)["principles.claude.md"]

    # The bare ``@`` import lives in the canonical phrase Principle 0
    # uses. Claude follows it transitively, so the adapter must keep it.
    assert "@.ctxr-fsm/memory/bootstrap.md to ensure" in rendered, rendered

    # No inline-bootstrap fence markers in the Claude adapter — the
    # whole point is that Claude's transitive ``@`` follow handles it.
    assert gen_mod.BOOTSTRAP_INLINE_BEGIN not in rendered  # type: ignore[attr-defined]
    assert gen_mod.BOOTSTRAP_INLINE_END not in rendered  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "filename",
    ["principles.codex.md", "principles.cursor.md"],
)
def test_non_claude_adapter_inlines_bootstrap_body(
    gen_mod: object, filename: str
) -> None:
    """Codex and Cursor adapters splice the bootstrap body into Principle 0."""

    rendered = _render_all(gen_mod)[filename]

    # Both fence markers must appear, in order.
    begin_marker: str = gen_mod.BOOTSTRAP_INLINE_BEGIN  # type: ignore[attr-defined]
    end_marker: str = gen_mod.BOOTSTRAP_INLINE_END  # type: ignore[attr-defined]
    assert begin_marker in rendered, rendered
    assert end_marker in rendered, rendered
    assert rendered.index(begin_marker) < rendered.index(end_marker)

    # A recognisable substring from bootstrap.md must appear inside
    # the inlined block — verifies the BODY landed, not just the
    # markers.
    begin_pos = rendered.index(begin_marker)
    end_pos = rendered.index(end_marker)
    inlined_block = rendered[begin_pos:end_pos]
    assert (
        "Step 1 — detect the package, then ASK ONCE before installing if missing"
        in inlined_block
    ), inlined_block

    # The per-client reload subsection (Gap 1 addition) must also be
    # visible inside the inlined block.
    assert "Per-client reload semantics" in inlined_block, inlined_block


@pytest.mark.parametrize(
    "filename",
    ["principles.codex.md", "principles.cursor.md"],
)
def test_non_claude_adapter_rewrites_at_reference_phrase(
    gen_mod: object, filename: str
) -> None:
    """Codex / Cursor backtick-quote the ``@`` reference + flag inlining.

    The surrounding human-facing prose still mentions the canonical
    path so a Claude-trained operator reading a Codex AGENTS.md can
    find the file. We don't strip the ``@`` reference entirely — we
    just stop using it as a load-bearing import (it becomes a
    breadcrumb).
    """

    rendered = _render_all(gen_mod)[filename]

    # The rewritten phrase (backticked path + inline note) must appear.
    assert (
        "`@.ctxr-fsm/memory/bootstrap.md` (inlined below for this client)"
        in rendered
    ), rendered

    # The bare unquoted ``@...to ensure`` phrase from the canonical
    # principles must NOT survive (that's what we rewrote).
    assert "@.ctxr-fsm/memory/bootstrap.md to ensure" not in rendered, rendered


def test_codex_adapter_demotes_bootstrap_h1_to_h3(gen_mod: object) -> None:
    """Bootstrap's ``# title`` becomes ``### title`` inside the inlined block."""

    rendered = _render_all(gen_mod)["principles.codex.md"]
    title_text = "ctxr-fsm bootstrap: how a skill or agent ensures fsm is ready"

    # After demotion by two levels the title must read as H3.
    assert f"### {title_text}" in rendered, rendered

    # The original H1 form (``# <title>`` at line start) MUST NOT
    # appear — would mean demotion didn't happen. We grep line-by-line
    # since ``# X`` is a substring of ``### X``.
    h1_title_lines = [
        line
        for line in rendered.splitlines()
        if line.startswith(f"# {title_text}")
    ]
    assert h1_title_lines == [], h1_title_lines


def test_codex_adapter_demotes_step_headings_to_h4(gen_mod: object) -> None:
    """``## Step 1`` in bootstrap.md becomes ``#### Step 1`` in the codex adapter."""

    rendered = _render_all(gen_mod)["principles.codex.md"]
    step_title = (
        "Step 1 — detect the package, then ASK ONCE before installing if missing"
    )
    assert f"#### {step_title}" in rendered, rendered
    # The original H2 form must not appear as a line-start heading.
    # We grep line-by-line rather than substring-matching because
    # ``## Step 1`` is a substring of ``#### Step 1`` and would
    # spuriously match.
    h2_step_lines = [
        line
        for line in rendered.splitlines()
        if line.startswith(f"## {step_title}")
    ]
    assert h2_step_lines == [], h2_step_lines


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_generator_is_idempotent(gen_mod: object, tmp_path: Path) -> None:
    """Running the generator twice produces byte-identical adapter files."""

    # Run main() once — the adapter files on disk now reflect the
    # current canonical. Capture their bytes.
    rc = gen_mod.main()  # type: ignore[attr-defined]
    assert rc == 0
    first: dict[str, bytes] = {
        adapter.filename: (gen_mod.MEMORY_DIR / adapter.filename).read_bytes()  # type: ignore[attr-defined]
        for adapter in gen_mod.ADAPTERS  # type: ignore[attr-defined]
    }

    # Run again — same input, same output.
    rc = gen_mod.main()  # type: ignore[attr-defined]
    assert rc == 0
    second: dict[str, bytes] = {
        adapter.filename: (gen_mod.MEMORY_DIR / adapter.filename).read_bytes()  # type: ignore[attr-defined]
        for adapter in gen_mod.ADAPTERS  # type: ignore[attr-defined]
    }

    assert first == second
