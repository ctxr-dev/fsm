"""Smoke test: the ``live_project`` fixture spins up real subsystems.

Pure fixture exercise, no UI assertions. Sits in front of the heavier
UI tests so a fixture-side regression (ensure failure, port collision,
subsystem-not-started) surfaces with a clear error rather than as a
cryptic Playwright timeout.
"""

from __future__ import annotations

import urllib.request

import pytest

from tests.integration.e2e.conftest import LiveProject


@pytest.mark.e2e
def test_live_project_yields_reachable_subsystems(live_project: LiveProject) -> None:
    assert live_project.project_root.is_dir()
    assert (live_project.project_root / ".ctxr-fsm" / "fsm.db").is_file()
    assert live_project.api_url.startswith("http://")
    assert live_project.ui_url.startswith("http://")

    # Each subsystem must answer healthz.
    with urllib.request.urlopen(f"{live_project.api_url}/healthz", timeout=10) as resp:
        assert resp.status == 200
    # UI dev server (Vite) does not have a /healthz route; we hit the
    # root and accept any 2xx as proof it's serving.
    with urllib.request.urlopen(live_project.ui_url, timeout=10) as resp:
        assert 200 <= resp.status < 300
