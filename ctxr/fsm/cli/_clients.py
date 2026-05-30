"""Shared client + ensure-pipeline enums consumed by the W14 CLI surface.

The bootstrap pipeline (``ensure``), the per-client MCP installer
(``install-mcp``), and the memory installer (``install-memory``) all
talk about the same closed vocabularies — which MCP client to target,
what status to report for each ensure step, what wire-shape the
top-level ``status`` field can take. Before W14i those vocabularies
were free-form string tuples re-declared in each command module
(``_CLIENT_CHOICES``, ``_MODE_CHOICES``, etc.). This module is the
single source of truth: defining the enum once means a new client / a
renamed status surfaces as a typed change everywhere the vocabulary is
read.

Wire-format note
----------------
``EnsureStatus`` previously emitted hyphen-prefixed ``"missing:init"`` /
``"missing:supervisor"`` / etc. on the top-level ``status`` field.
StrEnum members cannot contain colons, so W14i swaps the wire format to
underscored snake_case (``"missing_init"``, ``"missing_supervisor"``, …)
— the consistency win is worth the one-time cosmetic change.

``EnsureActionStatus`` keeps every legacy wire value byte-identical;
the enum is purely a typing tightening.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "EnsureActionStatus",
    "EnsureMode",
    "EnsureStatus",
    "McpClient",
    "McpConfigStatus",
]


class McpClient(StrEnum):
    """Identifier for an MCP client config target.

    Shared across :mod:`ctxr.fsm.cli.install_mcp_cmd`,
    :mod:`ctxr.fsm.cli.install_memory_cmd`, and
    :mod:`ctxr.fsm.cli.ensure_cmd` so the ``--client`` flag accepts the
    same vocabulary on every subcommand.

    Members:

    * ``auto``   — detect every supported client present in the target.
    * ``claude`` — Claude Code (``.mcp.json`` / ``.claude/settings.json`` /
      ``CLAUDE.md`` / ``.claude/CLAUDE.md``).
    * ``codex``  — Codex / OpenAI Agents SDK (``~/.codex/config.toml`` /
      ``AGENTS.md``).
    * ``cursor`` — Cursor (``~/.cursor/mcp.json`` / ``.cursor/rules/``).
    * ``none``   — no-op (skip the step entirely; useful for
      ``--no-mcp-config`` plumbing and headless CI).

    ``install-memory`` does NOT accept ``none`` historically; callers
    that need a memory-skip path pass ``--no-memory`` instead. The
    member is still defined here because the MCP-config + ensure paths
    do accept it.
    """

    auto = "auto"
    claude = "claude"
    codex = "codex"
    cursor = "cursor"
    none = "none"


class McpConfigStatus(StrEnum):
    """Result of ``install-mcp --check`` for one client.

    Members:

    * ``installed``   — the on-disk entry matches the desired stdio
      shape exactly.
    * ``missing``     — no entry present for ``ctxr-fsm`` in the
      target file.
    * ``out_of_date`` — entry present but differs from the desired
      shape (wrong command / wrong args / drift from current
      packaging).
    * ``unchanged``   — applied path: the file already matched and we
      did not write.

    Wire-format note: the legacy ``"out-of-date"`` (hyphenated) string
    is replaced with the snake-case ``"out_of_date"`` so the enum
    member name matches the wire value. The hyphenated literal is no
    longer emitted; consumers that grep for it must update.
    """

    installed = "installed"
    missing = "missing"
    out_of_date = "out_of_date"
    unchanged = "unchanged"


class EnsureActionStatus(StrEnum):
    """Status surfaced on each entry of the ensure ``actions`` dict.

    Members:

    * ``applied``   — the step ran and mutated state.
    * ``current``   — the step's substrate was already up to date.
    * ``skipped``   — the step was disabled (``--no-memory`` etc.) or
      the step is not relevant in this mode.
    * ``unchanged`` — the step ran but produced no diff; equivalent
      semantically to ``current`` for the install-mcp path.
    * ``reused``    — supervisor singleton was already alive and
      healthy.
    * ``spawned``   — supervisor singleton was started by this call.
    * ``failed``    — the step raised; ``failure_detail`` on the
      top-level summary explains why.
    * ``missing``   — ``--check`` mode only: the step would need to
      run to bring the substrate to ``ready``.
    """

    applied = "applied"
    current = "current"
    skipped = "skipped"
    unchanged = "unchanged"
    reused = "reused"
    spawned = "spawned"
    failed = "failed"
    missing = "missing"


class EnsureStatus(StrEnum):
    """Top-level ``status`` field on the ensure summary.

    Members:

    * ``ready``               — every required step is applied / current /
      reused and every required subsystem is healthz-passing.
    * ``degraded``            — apply path completed but at least one
      subsystem failed its post-spawn healthz probe.
    * ``failed``              — one of the ensure steps raised; the
      summary carries ``failure_detail``.
    * ``missing_init``        — ``--check`` mode: ``.ctxr-fsm/`` /
      DB / alembic is not current.
    * ``missing_supervisor``  — ``--check`` mode: supervisor isn't up.
    * ``missing_mcp_config``  — ``--check`` mode: client MCP config
      is missing or out of date.
    * ``missing_memory``      — ``--check`` mode: AI-client memory
      is missing or out of date.

    Wire-format change (W14i): the legacy ``"missing:init"``,
    ``"missing:supervisor"``, ``"missing:mcp-config"``,
    ``"missing:memory"`` colon-and-hyphen mixed strings are replaced by
    snake-cased single-token values (``"missing_init"``,
    ``"missing_supervisor"``, ``"missing_mcp_config"``,
    ``"missing_memory"``). StrEnum members cannot contain colons, and
    snake_case matches the convention used by every other status enum
    in the codebase. Callers parsing the JSON ``status`` field must
    update; the W14i PR description lists this change explicitly.
    """

    ready = "ready"
    degraded = "degraded"
    failed = "failed"
    missing_init = "missing_init"
    missing_supervisor = "missing_supervisor"
    missing_mcp_config = "missing_mcp_config"
    missing_memory = "missing_memory"


class EnsureMode(StrEnum):
    """Scope flag for ``ctxr-fsm ensure``.

    * ``full``     — bring up MCP + API + UI (default; the dashboard
      is part of the deliverable).
    * ``mcp_only`` — headless CI mode: bring up the MCP server only.

    Wire-format note: the canonical CLI flag value is the underscored
    ``--mode mcp_only`` so it matches the enum member's ``.value``. The
    Typer command continues to accept the legacy hyphenated form
    ``--mode mcp-only`` through ``ensure_cmd._MODE_ALIASES`` and emits
    a one-line deprecation warning to stderr when it sees it; the
    hyphen form is slated for removal in a future minor release. Docs
    + tests + new code should always use the underscore form.
    """

    full = "full"
    mcp_only = "mcp_only"


