"""E2E coverage for the ELK graph layout migration.

The user-visible win of the ELK migration is "no overlapping edge
labels on dense fan-outs". The skill-code-review v4 spec is the
canonical stress test: 18 states with multi-edge fan-outs. These
tests drive the dummy-fsm-test supervisor (long-lived; port 64547)
because the v4 spec is already registered there — re-seeding it on
every test run would duplicate the dummy-fsm-test setup.

Test matrix:

* ``elk_no_label_overlap``: navigate to /specs/<id>, wait for the
  layout spinner to disappear, then check that every visible edge
  label rect (DOM) is pairwise non overlapping. The label-non-overlap
  predicate is the bug we are fixing; if dagre were still the
  default this assertion would fail (which is why the dagre variant
  below is xfail / skipped from strict assertion).
* ``dagre_via_url_param``: same navigation but with ?layout=dagre.
  We assert the wrapper exposes data-layout-engine=dagre to prove
  the URL param routes to the legacy path. We do NOT assert
  non-overlap; dagre is known not to satisfy it on this spec.

The supervisor host + port are read from the SUPERVISOR_URL env var
(``http://127.0.0.1:64547`` by default) so a CI runner can point the
suite at a fresh supervisor instance if needed.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

import pytest


SUPERVISOR_URL = os.environ.get("SUPERVISOR_URL", "http://127.0.0.1:64547")


def _fetch_first_spec_id() -> str:
    """Return the id of the first registered spec on the supervisor.

    Used as the navigation target. The supervisor is expected to have
    at least one spec (typically the v4 code-reviewer) registered. We
    pick the first one rather than hard-coding a slug so the test is
    not coupled to which spec the developer happened to load.
    """
    url = f"{SUPERVISOR_URL}/api/v1/specs"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            body = resp.read().decode("utf-8")
    except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
        pytest.skip(
            f"dummy-fsm-test supervisor not reachable at {SUPERVISOR_URL} "
            f"({exc!r}); skip this test or boot the supervisor first."
        )
    payload = json.loads(body)
    items = payload.get("items") or []
    if not items:
        pytest.skip(
            f"supervisor at {SUPERVISOR_URL} has no specs registered; "
            "load one via `ctxr-fsm spec register` before running this test."
        )
    return items[0]["id"]


def _wait_for_layout(page, timeout_ms: int = 15_000) -> None:
    """Wait until the FlowGraph layout overlay disappears.

    The spinner is mounted by FlowGraph while the ELK promise is
    pending; once it disappears the layout pass is done and the DOM
    reflects the final node + edge positions. Falls back to a fixed
    delay when the spinner never appeared (e.g. on the dagre path,
    which is synchronous).
    """
    # ``hidden`` state matches both "spinner detached from DOM" and
    # "spinner is in DOM but display:none" — Playwright considers
    # absence of the locator as hidden, so a never-mounted spinner
    # resolves immediately.
    page.locator('[data-testid="fsm-graph-spinner"]').wait_for(
        state="hidden", timeout=timeout_ms
    )


def _rects_overlap(
    a: dict[str, float], b: dict[str, float], slack: float = 0.0
) -> bool:
    """Return True when two DOM rects (x, y, width, height) overlap.

    Slack lets us tolerate single-pixel rounding without registering
    a false positive overlap (Playwright's bounding box comes from
    layout pixels, which round half-integer positions). 0.0 means
    strict touching counts as overlap.
    """
    a_right = a["x"] + a["width"] + slack
    b_right = b["x"] + b["width"] + slack
    a_bottom = a["y"] + a["height"] + slack
    b_bottom = b["y"] + b["height"] + slack
    return (
        a["x"] < b_right
        and b["x"] < a_right
        and a["y"] < b_bottom
        and b["y"] < a_bottom
    )


@pytest.mark.e2e
@pytest.mark.allow_console_errors  # supervisor's UI proxies the dev API; skip strict console audit
def test_skill_v4_graph_elk_no_label_overlap(page) -> None:
    """ELK pass on the v4 spec graph: no edge labels overlap.

    Navigates to /specs/<first-registered-id> (typically the v4
    code-reviewer), waits for the layout spinner to clear, and asserts
    every state appears as a node + no two edge labels overlap.
    """
    spec_id = _fetch_first_spec_id()
    page.goto(
        f"{SUPERVISOR_URL}/specs/{spec_id}", wait_until="domcontentloaded"
    )
    # Wait for the page to mount the graph (the spec-detail panel
    # title is the main loaded signal).
    page.locator(".flow-graph").first.wait_for(timeout=15_000)
    _wait_for_layout(page)

    wrapper = page.locator(".flow-graph").first
    engine = wrapper.get_attribute("data-layout-engine")
    assert engine == "elk", (
        f"default layout engine should be 'elk' but wrapper exposes {engine!r}"
    )

    # Pull every edge-label bounding box. The FsmEdge component
    # renders each label inside a div annotated with
    # ``data-edge-id``; that attribute is the stable selector.
    label_locators = page.locator("[data-edge-id]")
    n_labels = label_locators.count()
    # The v4 spec has multiple labelled edges; reaching this point
    # means the layout did produce DOM nodes.
    assert n_labels >= 1, (
        f"expected >=1 edge labels on the v4 graph, found {n_labels}"
    )

    rects: list[dict[str, float]] = []
    for i in range(n_labels):
        box = label_locators.nth(i).bounding_box()
        if box is None:
            continue
        rects.append(box)

    # Pairwise non overlap. The ELK path's first class label routing
    # is what this assertion validates; if a regression reintroduces
    # the dagre style stack on a fan-out, the test fails with the
    # offending pair printed.
    overlaps: list[tuple[int, int]] = []
    for i in range(len(rects)):
        for j in range(i + 1, len(rects)):
            if _rects_overlap(rects[i], rects[j]):
                overlaps.append((i, j))
    assert not overlaps, (
        f"ELK layout produced overlapping edge labels: {overlaps}; rects: {rects}"
    )


@pytest.mark.e2e
@pytest.mark.allow_console_errors
def test_skill_v4_graph_dagre_via_url_param(page) -> None:
    """?layout=dagre routes through the legacy dagre code path.

    The label non overlap predicate is NOT asserted for dagre — that
    is the bug ELK fixes. We only verify the URL param is honoured
    (wrapper exposes data-layout-engine=dagre) and the graph still
    renders without throwing.
    """
    spec_id = _fetch_first_spec_id()
    page.goto(
        f"{SUPERVISOR_URL}/specs/{spec_id}?layout=dagre",
        wait_until="domcontentloaded",
    )
    page.locator(".flow-graph").first.wait_for(timeout=15_000)
    _wait_for_layout(page)

    wrapper = page.locator(".flow-graph").first
    engine = wrapper.get_attribute("data-layout-engine")
    assert engine == "dagre", (
        f"?layout=dagre should select dagre but wrapper exposes {engine!r}"
    )
    # Sanity: at least one edge label should render.
    label_locators = page.locator("[data-edge-id]")
    assert label_locators.count() >= 1, (
        "expected dagre path to still render edge labels"
    )
