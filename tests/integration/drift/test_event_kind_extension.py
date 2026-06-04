"""The closed EventKind taxonomy carries the new long-LLM-friendliness members.

``worker_dispatched`` already existed as an enum member but was never
emitted; the fix wires emission from ``fsm.get_brief``. ``heartbeat``
and ``drift_pause_cleared`` are new additions in this fix.
"""

from __future__ import annotations

from ctxr.fsm.core.models import EventKind


def test_worker_dispatched_member_present() -> None:
    assert EventKind.worker_dispatched.value == "worker_dispatched"


def test_heartbeat_member_present() -> None:
    assert EventKind.heartbeat.value == "heartbeat"


def test_drift_pause_cleared_member_present() -> None:
    assert EventKind.drift_pause_cleared.value == "drift_pause_cleared"


def test_event_kind_is_str_enum() -> None:
    """StrEnum membership: ``EventKind.heartbeat == "heartbeat"`` must hold.

    The wire shape (canonical JSON payloads) collapses StrEnum to str,
    so legacy callers comparing with raw strings keep working.
    """
    assert EventKind.heartbeat == "heartbeat"
    assert EventKind.drift_pause_cleared == "drift_pause_cleared"
    assert EventKind.worker_dispatched == "worker_dispatched"
