"""Closed-vocabulary enums shared across MCP tool modules.

The W14i extract-StrEnum sweep placed each new enum in the FIRST tool
module that consumed it. That co-location is the right default — most
enums stay single-module. The exception is :class:`JournalAction`,
which describes the recovery action a pending journal_txn can take.
The vocabulary is referenced both by

* :mod:`ctxr.fsm.mcp.tools_runs` (``ResumeRunInput.journal``,
  ``commit_outputs`` resume path), and

* :mod:`ctxr.fsm.mcp.tools_events` (the dedicated
  ``recover_journal`` tool surface).

Per W14i's new coding-standard rule (``docs/coding-standards.md``,
"Promote per-tool enums on second-module use"), shared MCP enums live
here so neither tool module has to import the other just to share a
vocabulary. Importing across tool modules would also couple their
import-time side effects — having ``tools_events`` reach into
``tools_runs`` for an enum drags the entire ``tools_runs`` import
graph into the ``tools_events`` boot path, which is a regression
against the W3 "each tool module stands alone" rule.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["JournalAction"]


class JournalAction(StrEnum):
    """Action a resume / journal-recovery call may take on a pending tx.

    Surfaced on ``ResumeRunInput.journal`` (in
    :mod:`ctxr.fsm.mcp.tools_runs`) and on the dedicated
    journal-recovery MCP/CLI surface (in
    :mod:`ctxr.fsm.mcp.tools_events`). ``discard`` rolls the pending
    journal_txn back; ``replay`` finalises its staged writes.
    """

    discard = "discard"
    replay = "replay"
