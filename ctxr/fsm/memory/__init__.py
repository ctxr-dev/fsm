"""Canonical FSM-usage principles shipped with the ctxr-fsm package.

This package directory holds the source-of-truth principles file
(`principles.md`) plus per-client adapters generated from it
(`principles.claude.md`, `principles.codex.md`, `principles.cursor.md`).

The adapters are byte-stable re-emits of `principles.md` with the right
per-client frontmatter and a generated-from header. Regenerate via:

    uv run python tools/generate_memory_adapters.py
"""

from __future__ import annotations

from pathlib import Path

PRINCIPLES_DIR: Path = Path(__file__).parent

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


__all__ = ["PRINCIPLES_DIR", "get_principles_path"]
