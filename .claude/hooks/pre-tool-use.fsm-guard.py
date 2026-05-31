#!/usr/bin/env python3
"""Pre-tool-use hook: enforce the active FSM run's allowed_tools list.

This is the W12 layer-4 reference implementation. When ``ctxr-fsm``
publishes an ``active-run.json`` marker under ``.ctxr-fsm/`` in the
project tree, this hook reads it on every Claude Code tool invocation
and rejects any tool that is not in the union of:

* the current FSM state's ``allowed_tools`` list, plus
* the implicit ``fsm.*`` family (so the worker can always call
  ``fsm.commit_outputs`` / ``fsm.confirm_commit`` etc. to advance the
  run regardless of the state's declared toolbelt).

When no marker is present (no FSM run active), the hook exits 0 and
all tool calls are allowed — installing the hook must NEVER make a
Claude Code session unusable just because there is no FSM run live.

Hook contract (Claude Code)
---------------------------

Claude Code feeds the hook a JSON payload on stdin shaped like::

    {"tool_name": "Bash", "tool_input": {...}, ...}

The hook MUST exit 0 to allow the tool call. Exiting non-zero blocks
the call; anything we write to ``stderr`` is surfaced to the agent /
user verbatim. We write a structured JSON blob so an upstream parser
can route on it::

    {"blocked": true, "reason": "...", "tool": "...", "allowed": [...]}

The hook is intentionally dependency-free: it uses only the Python
standard library so a consumer project can drop it under
``.claude/hooks/`` without installing ``ctxr-fsm`` itself. Discovery
of the marker walks the directory tree upwards from
``$CLAUDE_PROJECT_DIR`` (or the cwd, as a fallback) looking for the
nearest ``.ctxr-fsm/active-run.json`` — the same layout the
``ctxr-fsm`` supervisor / doctor / lifecycle primitives use.

Exit codes
----------

* ``0`` — allow.
* ``1`` — block; structured JSON reason on stderr.
* ``0`` (also) — soft-fail: any unexpected internal error (corrupt
  marker, IO error, malformed payload) falls back to "allow" so a
  buggy hook never bricks the agent. The stderr line still names the
  failure so an operator can diagnose it.
"""

from __future__ import annotations

import fnmatch
import json
import os
import sys
from pathlib import Path
from typing import Any

# Name of the marker file written by ``write_active_run_marker``.
# Duplicated (rather than imported from ctxr) so a consumer project
# can drop this hook in place without taking a Python dependency on
# the FSM package itself.
_MARKER_RELPATH = Path(".ctxr-fsm") / "active-run.json"

# Tool name pattern that is ALWAYS allowed regardless of the current
# state's allowlist — the worker needs ``fsm.*`` to drive lifecycle.
_ALWAYS_ALLOWED: tuple[str, ...] = ("fsm.*",)


def _project_root_candidates() -> list[Path]:
    """Return directories that may hold the ``.ctxr-fsm/`` subtree.

    Claude Code sets ``$CLAUDE_PROJECT_DIR`` to the project root for
    every tool-use hook; we prefer that. The cwd is a fallback for
    direct invocations (tests, manual ``echo ... | hook.py`` calls).
    Both are tried; the first to contain the marker wins.
    """
    candidates: list[Path] = []
    env_root = os.environ.get("CLAUDE_PROJECT_DIR")
    if env_root:
        candidates.append(Path(env_root).expanduser().resolve())
    candidates.append(Path.cwd().resolve())
    return candidates


def _find_marker(starts: list[Path]) -> Path | None:
    """Walk each start directory upwards looking for ``.ctxr-fsm/active-run.json``.

    Stops at the filesystem root. Returns the first marker found, or
    ``None`` if none of the candidates have one. We walk upwards so a
    Claude Code session started in a subdirectory of the project still
    finds the project-root marker.
    """
    seen: set[Path] = set()
    for start in starts:
        cursor = start
        while True:
            if cursor in seen:
                break
            seen.add(cursor)
            candidate = cursor / _MARKER_RELPATH
            if candidate.is_file():
                return candidate
            if cursor.parent == cursor:
                break
            cursor = cursor.parent
    return None


def _load_marker(path: Path) -> dict[str, Any] | None:
    """Return the parsed marker, or ``None`` on missing/malformed.

    "Missing" and "malformed" both fall through to "no enforcement" so
    a hand-edit that breaks the JSON cannot brick the agent. The
    caller logs the corruption to stderr so an operator can notice.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(loaded, dict):
        return None
    return loaded


def _matches_any(tool_name: str, patterns: list[str]) -> bool:
    """Return ``True`` iff ``tool_name`` matches any glob in ``patterns``.

    ``fnmatch`` glob semantics — ``fsm.*`` matches ``fsm.commit_outputs``;
    ``Read`` matches only ``Read``. Empty pattern list means "nothing
    allowed" (caller is responsible for short-circuiting on no marker).
    """
    return any(fnmatch.fnmatchcase(tool_name, pat) for pat in patterns)


def _read_payload() -> dict[str, Any]:
    """Parse the JSON payload Claude Code writes to stdin.

    Returns an empty dict on parse error / empty stdin — the caller
    treats a missing ``tool_name`` as "unknown" and allows the call so
    a payload-shape change in Claude Code never wedges the agent.
    """
    try:
        raw = sys.stdin.read()
    except OSError:
        return {}
    if not raw.strip():
        return {}
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(loaded, dict):
        return {}
    return loaded


def _block(tool: str, allowed: list[str], reason: str) -> int:
    """Emit a structured block payload on stderr and return exit code 1."""
    payload = {
        "blocked": True,
        "tool": tool,
        "allowed": allowed,
        "reason": reason,
    }
    sys.stderr.write(json.dumps(payload))
    sys.stderr.write("\n")
    return 1


def main() -> int:
    """Read marker + payload, decide allow / block.

    Soft-fail policy: if anything unexpected happens (no marker, bad
    JSON, missing fields) we ALLOW the call. The hook exists to
    enforce a *known* allowlist; without one the safe default is to
    stay out of the way.
    """
    payload = _read_payload()
    tool_name = str(payload.get("tool_name") or "")

    marker_path = _find_marker(_project_root_candidates())
    if marker_path is None:
        # No FSM run active — allow.
        return 0

    marker = _load_marker(marker_path)
    if marker is None:
        sys.stderr.write(
            f"fsm-guard: marker at {marker_path} unreadable; allowing tool call\n"
        )
        return 0

    raw_allowed = marker.get("allowed_tools")
    if not isinstance(raw_allowed, list):
        # No allowlist => unrestricted run.
        return 0

    # An empty allowlist means "no state-imposed restriction" — fail
    # open (still allow ``fsm.*``-only is too aggressive; an empty list
    # should mirror "no marker" semantics).
    if not raw_allowed:
        return 0

    allowed: list[str] = [str(t) for t in raw_allowed] + list(_ALWAYS_ALLOWED)

    if not tool_name:
        # No tool name in payload — can't enforce; allow.
        return 0

    if _matches_any(tool_name, allowed):
        return 0

    run_id = str(marker.get("run_id") or "<unknown>")
    current_state = str(marker.get("current_state") or "<unknown>")
    reason = (
        f"tool {tool_name!r} not in active FSM run's allowed_tools "
        f"(run_id={run_id}, state={current_state}, allowed={allowed})"
    )
    return _block(tool_name, allowed, reason)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        # Treat Ctrl-C as "do not block" — the operator is already
        # cancelling, blocking on top of that would be noise.
        sys.exit(0)
    except Exception as exc:  # pragma: no cover - defensive soft-fail
        sys.stderr.write(f"fsm-guard: internal error {exc!r}; allowing tool call\n")
        sys.exit(0)
