"""Canonical FSM-usage principles + bootstrap doc shipped with the ctxr-fsm package.

This package directory holds:

* the source-of-truth principles file (``principles.md``) plus per-client
  adapters generated from it (``principles.claude.md``,
  ``principles.codex.md``, ``principles.cursor.md``).
* the source-of-truth bootstrap doc (``bootstrap.md``) describing how a
  skill / agent should ensure ``ctxr-fsm`` is ready before any work.
  The repo-root ``BOOTSTRAP.md`` is a generated mirror of this file
  (see ``scripts/sync_bootstrap_doc.py``).

The adapters are byte-stable re-emits of ``principles.md`` with the
right per-client frontmatter and a generated-from header. Regenerate
via:

    uv run python tools/generate_memory_adapters.py
"""

from __future__ import annotations

from pathlib import Path

PRINCIPLES_DIR: Path = Path(__file__).parent

# Filename of the bootstrap doc inside this package, kept as a module
# constant so the installer + tests + sync script all reference the
# same symbol rather than open-coding the string.
BOOTSTRAP_FILENAME: str = "bootstrap.md"

_CLIENT_FILENAMES: dict[str, str] = {
    "canonical": "principles.md",
    "claude": "principles.claude.md",
    "codex": "principles.codex.md",
    "cursor": "principles.cursor.md",
}


def get_principles_path(client: str = "claude") -> Path:
    """Return the absolute path to the principles file for ``client``.

    Parameters
    ----------
    client:
        One of ``"canonical"``, ``"claude"``, ``"codex"``, ``"cursor"``.
        Defaults to ``"claude"``.

    Raises
    ------
    ValueError
        If ``client`` is not a recognised key.
    FileNotFoundError
        If the requested file is not present on disk.
    """

    try:
        filename = _CLIENT_FILENAMES[client]
    except KeyError as exc:
        valid = ", ".join(sorted(_CLIENT_FILENAMES))
        raise ValueError(
            f"unknown client {client!r}; expected one of: {valid}"
        ) from exc

    path = PRINCIPLES_DIR / filename
    if not path.is_file():
        raise FileNotFoundError(
            f"principles file missing for client={client!r}: {path}"
        )
    return path


def get_bootstrap_path() -> Path:
    """Return the absolute path to the canonical ``bootstrap.md`` doc.

    The bootstrap doc is client-agnostic (every skill / agent reads the
    same text, regardless of which MCP client hosts it), so there is no
    per-client variant — one file, one path.

    Raises
    ------
    FileNotFoundError
        If the bootstrap doc is missing from the installed package — a
        packaging bug the caller should surface rather than paper over.
    """

    path = PRINCIPLES_DIR / BOOTSTRAP_FILENAME
    if not path.is_file():
        raise FileNotFoundError(f"bootstrap doc missing in package: {path}")
    return path


__all__ = [
    "BOOTSTRAP_FILENAME",
    "PRINCIPLES_DIR",
    "get_bootstrap_path",
    "get_principles_path",
]
