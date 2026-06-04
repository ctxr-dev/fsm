"""The default idle window is 300s + ``CTXR_FSM_IDLE_TIMEOUT_SECONDS`` override.

Long-LLM-friendliness: the 60s pre-fix default tripped legitimately-
slow workers (large codebase scans, deep analyses). The new default
is 300s; operators tune via the env var when their workloads need a
different budget.
"""

from __future__ import annotations

from ctxr.fsm.sqlite.drift import (
    DEFAULT_IDLE_WINDOW_SECONDS,
    IDLE_TIMEOUT_ENV_VAR,
    DriftConfig,
)


def test_default_idle_window_is_five_minutes() -> None:
    """A freshly-constructed DriftConfig honours the 300s default."""
    cfg = DriftConfig()
    assert DEFAULT_IDLE_WINDOW_SECONDS == 300.0
    assert cfg.window_seconds == 300.0


def test_env_var_overrides_idle_window(monkeypatch) -> None:
    """``CTXR_FSM_IDLE_TIMEOUT_SECONDS`` overrides the default."""
    monkeypatch.setenv(IDLE_TIMEOUT_ENV_VAR, "120")
    cfg = DriftConfig()
    assert cfg.window_seconds == 120.0


def test_invalid_env_var_falls_back_to_default(monkeypatch) -> None:
    """Garbage / non-positive values fall back to the module default."""
    monkeypatch.setenv(IDLE_TIMEOUT_ENV_VAR, "not-a-number")
    cfg = DriftConfig()
    assert cfg.window_seconds == DEFAULT_IDLE_WINDOW_SECONDS

    monkeypatch.setenv(IDLE_TIMEOUT_ENV_VAR, "-5")
    cfg = DriftConfig()
    assert cfg.window_seconds == DEFAULT_IDLE_WINDOW_SECONDS

    monkeypatch.setenv(IDLE_TIMEOUT_ENV_VAR, "0")
    cfg = DriftConfig()
    assert cfg.window_seconds == DEFAULT_IDLE_WINDOW_SECONDS
