#!/usr/bin/env python3
"""Generate per-client memory adapters from the canonical principles file.

Reads ``ctxr/fsm/memory/principles.md`` and emits three sibling files:

* ``principles.claude.md``  -- Claude Code (CLAUDE.md, plain markdown)
* ``principles.codex.md``   -- Codex (AGENTS.md, plain markdown)
* ``principles.cursor.md``  -- Cursor (.mdc, with Cursor frontmatter)

The generator is idempotent: running it twice in a row produces
byte-identical files. The Claude adapter shares the canonical body
verbatim with a single generated-from HTML comment header — Claude
natively follows the ``@.ctxr-fsm/memory/bootstrap.md`` import in
Principle 0, so no inlining is needed there.

Codex and Cursor do NOT follow Claude's ``@<path>`` import syntax —
their LLMs see the literal string and have no way to fetch the
bootstrap content. To close that coverage gap the generator INLINES
the canonical bootstrap body into Principle 0 for those two adapters,
fenced by HTML markers (``bootstrap-content-begin`` /
``bootstrap-content-end``) so downstream tooling (drift detection,
tests) can recognise the inlined block. The surrounding human-facing
prose still references ``@.ctxr-fsm/memory/bootstrap.md`` so an
operator reading the adapter file can find the canonical path on
disk.

To keep heading hierarchy clean when the bootstrap H1 nests under
Principle 0 (H2), every heading in the inlined block is demoted by
two levels — bootstrap's ``# title`` becomes ``### title``,
``## Step 1`` becomes ``#### Step 1`` etc. The :func:`_demote_headings`
helper caps at H6 (HTML's max heading level).

Run from the repo root::

    uv run python tools/generate_memory_adapters.py

Exits 0 on success, non-zero on error.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parent.parent
MEMORY_DIR: Path = REPO_ROOT / "ctxr" / "fsm" / "memory"
CANONICAL: Path = MEMORY_DIR / "principles.md"
BOOTSTRAP: Path = MEMORY_DIR / "bootstrap.md"

GENERATED_HEADER: str = (
    "<!-- This file is generated from ctxr-fsm package memory. "
    "Run `ctxr-fsm install-memory --check` to confirm sync. -->\n"
)

CURSOR_FRONTMATTER: str = (
    "---\n"
    "description: ctxr-fsm FSM-usage discipline\n"
    'globs: ["**/*"]\n'
    "alwaysApply: false\n"
    "---\n"
)

# HTML-comment fences used to wrap the inlined bootstrap body inside
# the codex / cursor adapters. Kept as module constants so tests and
# downstream tooling can import and match against them rather than
# open-coding the strings.
BOOTSTRAP_INLINE_BEGIN: str = (
    "<!-- bootstrap-content-begin "
    "(inlined for clients that don't follow @ imports) -->"
)
BOOTSTRAP_INLINE_END: str = "<!-- bootstrap-content-end -->"

# The canonical ``@`` import reference embedded in Principle 0. We
# locate it exactly so we can rewrite its surrounding sentence for
# the codex / cursor adapters (Claude's adapter keeps the literal
# unchanged because Claude follows the import natively).
_BOOTSTRAP_REF: str = "@.ctxr-fsm/memory/bootstrap.md"

# The exact substring inside Principle 0 we replace for codex /
# cursor. Kept narrow so a future edit to surrounding prose doesn't
# break the rewrite silently — if the canonical changes shape, the
# replacement fails loudly via the assertion in
# :func:`_inline_bootstrap_into_principles`.
_BOOTSTRAP_REF_PHRASE: str = (
    f"follow the bootstrap procedure at {_BOOTSTRAP_REF} to ensure"
)
_BOOTSTRAP_REF_PHRASE_INLINED: str = (
    f"follow the bootstrap procedure at `{_BOOTSTRAP_REF}` "
    f"(inlined below for this client) to ensure"
)

# Regex matching any ATX heading from H1 through H5. H6 is excluded
# because HTML has no H7; promoting an H6 further would silently
# collapse to H6 and lose information. We document this cap in
# :func:`_demote_headings` and refuse to silently exceed it.
_HEADING_RE: re.Pattern[str] = re.compile(r"^(#{1,5}) ", flags=re.MULTILINE)


@dataclass(frozen=True)
class Adapter:
    """A per-client adapter file to emit.

    ``inline_bootstrap`` controls whether the bootstrap body is
    spliced into Principle 0 (True for clients that don't follow
    Claude's ``@<path>`` import syntax, False for Claude where the
    import resolves natively).
    """

    filename: str
    frontmatter: str  # extra YAML frontmatter prepended above the header
    body_prefix: str  # text inserted between frontmatter and canonical body
    inline_bootstrap: bool


ADAPTERS: tuple[Adapter, ...] = (
    Adapter(
        filename="principles.claude.md",
        frontmatter="",
        body_prefix=GENERATED_HEADER + "\n",
        inline_bootstrap=False,
    ),
    Adapter(
        filename="principles.codex.md",
        frontmatter="",
        body_prefix=GENERATED_HEADER + "\n",
        inline_bootstrap=True,
    ),
    Adapter(
        filename="principles.cursor.md",
        frontmatter=CURSOR_FRONTMATTER,
        body_prefix=GENERATED_HEADER + "\n",
        inline_bootstrap=True,
    ),
)


def _demote_headings(text: str, levels: int) -> str:
    """Demote every ATX heading in ``text`` by ``levels`` (cap at H6).

    Increases the leading ``#`` count of every H1..H5 heading by
    ``levels``, capping at H6 (HTML's max heading level — H7 is not
    a thing). H6 headings are passed through unchanged (already at
    the cap). The function only touches ATX-style headings
    (``# title``) at start-of-line; underline-style headings and
    in-paragraph ``#`` characters are not affected.

    Parameters
    ----------
    text:
        The markdown body to rewrite.
    levels:
        Non-negative integer count of heading levels to demote.

    Returns
    -------
    str
        ``text`` with all H1..H5 headings demoted, byte-identical
        to the input when ``levels == 0``.

    Raises
    ------
    ValueError
        If ``levels`` is negative — promotion (demote by a negative)
        is not supported and signals a caller bug.
    """

    if levels < 0:
        raise ValueError(f"levels must be >= 0, got {levels}")
    if levels == 0:
        return text

    def _replace(match: re.Match[str]) -> str:
        hashes = match.group(1)
        new_count = min(len(hashes) + levels, 6)
        return "#" * new_count + " "

    return _HEADING_RE.sub(_replace, text)


def _read_bootstrap_body() -> str:
    """Return the canonical bootstrap.md body, demoted for nesting under H2.

    Principle 0 in principles.md is an H2 (``## Principle 0:``). The
    bootstrap doc's title is an H1 (``# ctxr-fsm bootstrap: ...``).
    To keep the inlined block readable as a child of Principle 0 we
    demote every heading by two levels: the bootstrap title becomes
    an H3, its ``## Step 1`` subheadings become H4, etc.
    """

    if not BOOTSTRAP.is_file():
        raise FileNotFoundError(
            f"bootstrap doc missing at {BOOTSTRAP}; cannot inline"
        )
    body = BOOTSTRAP.read_text(encoding="utf-8")
    return _demote_headings(body, levels=2)


def _inline_bootstrap_into_principles(canonical_text: str) -> str:
    """Splice the demoted bootstrap body into Principle 0 of ``canonical_text``.

    Locates the literal ``follow the bootstrap procedure at
    @.ctxr-fsm/memory/bootstrap.md to ensure`` phrase inside
    Principle 0 and rewrites it to backtick-quote the path + flag
    that the bootstrap content is inlined below. Then appends the
    fenced bootstrap block at the END of the Principle 0 paragraph.

    Asserts the source phrase is present exactly once — a future
    edit to principles.md that breaks this assumption fails loudly
    here rather than silently emitting a broken adapter.
    """

    count = canonical_text.count(_BOOTSTRAP_REF_PHRASE)
    if count != 1:
        raise AssertionError(
            f"expected exactly one occurrence of bootstrap reference phrase "
            f"in principles.md, found {count}; canonical text may have "
            f"drifted from the inlining contract"
        )

    rewritten = canonical_text.replace(
        _BOOTSTRAP_REF_PHRASE, _BOOTSTRAP_REF_PHRASE_INLINED
    )

    bootstrap_body = _read_bootstrap_body()
    inlined_block = (
        f"\n{BOOTSTRAP_INLINE_BEGIN}\n\n"
        f"{bootstrap_body.rstrip(chr(10))}\n\n"
        f"{BOOTSTRAP_INLINE_END}\n"
    )

    # Splice the inlined block after the Principle 0 paragraph that
    # contains the reference. We find the FIRST blank line after the
    # rewritten phrase (which terminates the paragraph) and insert
    # the block there.
    phrase_pos = rewritten.find(_BOOTSTRAP_REF_PHRASE_INLINED)
    paragraph_end = rewritten.find("\n\n", phrase_pos)
    if paragraph_end == -1:
        # No following paragraph break — append the block at EOF.
        return rewritten.rstrip("\n") + "\n" + inlined_block

    return (
        rewritten[:paragraph_end]
        + "\n"
        + inlined_block
        + rewritten[paragraph_end:]
    )


def _render(canonical_text: str, adapter: Adapter) -> str:
    """Compose the final file contents for one adapter."""

    body = (
        _inline_bootstrap_into_principles(canonical_text)
        if adapter.inline_bootstrap
        else canonical_text
    )
    return f"{adapter.frontmatter}{adapter.body_prefix}{body}"


def _write_if_changed(path: Path, content: str) -> bool:
    """Write ``content`` to ``path``; return True if disk changed."""

    encoded = content.encode("utf-8")
    if path.is_file() and path.read_bytes() == encoded:
        return False
    path.write_bytes(encoded)
    return True


def main() -> int:
    if not CANONICAL.is_file():
        print(
            f"error: canonical principles file not found at {CANONICAL}",
            file=sys.stderr,
        )
        return 1

    canonical_text = CANONICAL.read_text(encoding="utf-8")

    changed: list[str] = []
    unchanged: list[str] = []
    for adapter in ADAPTERS:
        target = MEMORY_DIR / adapter.filename
        rendered = _render(canonical_text, adapter)
        if _write_if_changed(target, rendered):
            changed.append(adapter.filename)
        else:
            unchanged.append(adapter.filename)

    if changed:
        print(f"updated: {', '.join(changed)}")
    if unchanged:
        print(f"unchanged: {', '.join(unchanged)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
