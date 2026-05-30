#!/usr/bin/env python3
"""Generate per-client memory adapters from the canonical principles file.

Reads ``ctxr/fsm/memory/principles.md`` and emits three sibling files:

* ``principles.claude.md``  -- Claude Code (CLAUDE.md, plain markdown)
* ``principles.codex.md``   -- Codex (AGENTS.md, plain markdown)
* ``principles.cursor.md``  -- Cursor (.mdc, with Cursor frontmatter)

The generator is idempotent: running it twice in a row produces
byte-identical files. The Claude and Codex adapters share the canonical
body verbatim with a single generated-from HTML comment header. The
Cursor adapter wraps the same body with Cursor-specific YAML frontmatter
(``description``, ``globs``, ``alwaysApply``).

Run from the repo root::

    uv run python tools/generate_memory_adapters.py

Exits 0 on success, non-zero on error.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parent.parent
MEMORY_DIR: Path = REPO_ROOT / "ctxr" / "fsm" / "memory"
CANONICAL: Path = MEMORY_DIR / "principles.md"

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


@dataclass(frozen=True)
class Adapter:
    """A per-client adapter file to emit."""

    filename: str
    frontmatter: str  # extra YAML frontmatter prepended above the header
    body_prefix: str  # text inserted between frontmatter and canonical body


ADAPTERS: tuple[Adapter, ...] = (
    Adapter(
        filename="principles.claude.md",
        frontmatter="",
        body_prefix=GENERATED_HEADER + "\n",
    ),
    Adapter(
        filename="principles.codex.md",
        frontmatter="",
        body_prefix=GENERATED_HEADER + "\n",
    ),
    Adapter(
        filename="principles.cursor.md",
        frontmatter=CURSOR_FRONTMATTER,
        body_prefix=GENERATED_HEADER + "\n",
    ),
)


def _render(canonical_text: str, adapter: Adapter) -> str:
    """Compose the final file contents for one adapter."""

    return f"{adapter.frontmatter}{adapter.body_prefix}{canonical_text}"


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
