/**
 * Tests for routes/runDetail.tsx — PR 7 layout cutover.
 *
 * Coverage:
 *   - The 2-column grid (graph | timeline) renders at desktop width.
 *     We assert by ``data-testid`` plus the Tailwind classes that
 *     drive the layout (``grid-cols-1 lg:grid-cols-2``) — jsdom does
 *     not evaluate breakpoint media queries, but the class list IS the
 *     contract: at lg+ the browser flips to a 2-col grid.
 *   - Pre-cutover surfaces (States tree Card, inline Admin Card, the
 *     Tool Calls audit log Card, the per-node JsonViewer inspector)
 *     are ABSENT from the rendered tree. These three negative asserts
 *     are how a future cascade regression would surface — if any one
 *     of them rematerialises the test fails fast.
 *   - The "Admin" header button is present so the operator can still
 *     reach the journal / lock / drift / signatures / allowed-tools /
 *     tool-calls surfaces (they live inside AdminSheetBody now).
 */

import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { cleanup, render, waitFor } from '@testing-library/preact';

import type {
  RunDetail,
  StateNode,
} from '../../../lib/api';

// --- Mocks --------------------------------------------------------------

// preact-iso ``useRoute`` does not work outside a LocationProvider;
// the route only reads ``params.runId`` so a flat mock is enough.
vi.mock('preact-iso', () => ({
  useRoute: () => ({ params: { runId: 'run-test-1' } }),
}));

// The SSE EventStream opens a real EventSource on construction; jsdom
// doesn't ship one. Stub the module so the timeline mount doesn't
// throw and the route's render path is exercised end-to-end.
vi.mock('../../../lib/sse', () => ({
  EventStream: class {
    on(): () => void {
      return () => {
        // no-op
      };
    }
    close(): void {
      // no-op
    }
  },
}));

// Stub the global RunProgressGraph so the test doesn't have to drag in
// xyflow + dagre + spec graph layout. We render a sentinel div so the
// left column is identifiable by the layout test.
vi.mock('../../../components/RunProgressGraph', () => ({
  RunProgressGraph: () => (
    <div data-testid="mock-run-progress-graph">graph</div>
  ),
}));

// Capture the api mock so we can both seed responses and assert
// AdminSheet-feeding fetches still fire from the route (PR 7 keeps the
// fetch in the route so the sheet sees populated data on first open).
const ROOT_STATE: StateNode = {
  entry_id: 'entry-1',
  state_id: 'plan',
  entry_seq: 1,
  entered_at: '2025-01-01T00:00:00Z',
  exited_at: '2025-01-01T00:00:10Z',
  status: 'exited',
  inputs: {},
  outputs: {},
  iteration_n: 0,
  children: [],
};

const RUN: RunDetail = {
  manifest: {
    id: 'run-test-1',
    fsm_spec_id: 'spec-1',
    status: 'completed',
    verdict: 'pass',
    started_at: '2025-01-01T00:00:00Z',
    ended_at: '2025-01-01T00:01:00Z',
    last_update_at: '2025-01-01T00:01:00Z',
    transitions_count: 1,
    current_state: null,
  } as unknown as RunDetail['manifest'],
  state_tree: ROOT_STATE,
  journal: null,
  lock: null,
} as unknown as RunDetail;

vi.mock('../../../lib/api', async () => {
  class ApiErrorMock extends Error {}
  return {
    ApiError: ApiErrorMock,
    api: {
      getRun: vi.fn(async () => RUN),
      getStateTree: vi.fn(async () => ROOT_STATE),
      getEvents: vi.fn(async () => ({ items: [], total: 0 })),
      listToolCalls: vi.fn(async () => ({ items: [], total: 0 })),
      listDriftSignals: vi.fn(async () => ({ score: 0, signals: [] })),
      listCommitSignatures: vi.fn(async () => ({ items: [], total: 0 })),
      getSpec: vi.fn(async () => ({
        id: 'spec-1',
        project_id: 'p',
        project_slug: 'p',
        slug: 'demo',
        version: 1,
        hash: 'h',
        registered_at: '2025-01-01T00:00:00Z',
        definition: { entry: 'plan', states: [{ id: 'plan', kind: 'worker' }] },
      })),
      abortRun: vi.fn(),
      resumeRun: vi.fn(),
      recoverJournal: vi.fn(),
    },
  };
});

// Now import the route AFTER the mocks are set up.
import { RunDetailRoute } from '../../runDetail';

afterEach(() => cleanup());

function renderRoute() {
  // ``useToast`` is signal-backed and works without a provider — no
  // wrapper component needed.
  return render(<RunDetailRoute />);
}

describe('RunDetailRoute (PR 7 layout)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  test('renders the 50/50 grid (graph | timeline) once data has loaded', async () => {
    const { getByTestId } = renderRoute();

    const grid = await waitFor(() => getByTestId('run-detail-grid'));

    // The grid class list IS the contract — single column on small
    // screens, 2 columns at lg+. jsdom doesn't evaluate the breakpoint
    // but if the class disappears the responsive behaviour breaks.
    const cls = grid.getAttribute('class') ?? '';
    expect(cls).toContain('grid-cols-1');
    expect(cls).toContain('lg:grid-cols-2');

    // Both columns must render (graph mock + timeline body).
    expect(getByTestId('mock-run-progress-graph')).toBeInTheDocument();
    expect(getByTestId('run-event-timeline')).toBeInTheDocument();
  });

  test('mobile / small-viewport class list still drives a single column', async () => {
    // We can't change the jsdom viewport mid-test (jsdom is not a
    // layout engine) — but the route always emits BOTH ``grid-cols-1``
    // and ``lg:grid-cols-2``, and the browser applies the right rule
    // based on viewport width. This test pins the class contract so
    // a refactor can't accidentally drop the mobile fallback.
    const { getByTestId } = renderRoute();
    const grid = await waitFor(() => getByTestId('run-detail-grid'));
    expect(grid.className).toMatch(/\bgrid-cols-1\b/);
  });

  test('Admin button is present in the header', async () => {
    const { getByRole } = renderRoute();
    const adminBtn = await waitFor(() =>
      getByRole('button', { name: /Admin/i }),
    );
    expect(adminBtn).toBeInTheDocument();
  });

  test('legacy inline surfaces are not rendered (States tree, Admin Card, Tool Calls audit, per-node JsonViewer panels)', async () => {
    const { queryByText, getByTestId } = renderRoute();

    // Wait for the layout to settle first so the assertions below
    // race against fully-rendered content (not the loading Spinner).
    await waitFor(() => getByTestId('run-detail-grid'));

    // 1. Left States tree Card had a "States" Card title and a
    //    role="tree" landmark. Neither should appear in the new layout.
    expect(queryByText('States')).toBeNull();

    // 2. Inline Admin Card had section headers "Journal", "Lock",
    //    "Drift", "Last commit signatures", "Allowed tools". The
    //    canonical Journal / Drift labels are gone from the inline
    //    render — they only show up inside AdminSheetBody when the
    //    sheet is open.
    expect(queryByText('Journal')).toBeNull();
    expect(queryByText('Last commit signatures')).toBeNull();
    expect(queryByText('Allowed tools')).toBeNull();

    // 3. The bottom collapsible Tool Calls audit log header.
    expect(queryByText(/Tool calls audit log/i)).toBeNull();

    // 4. The per-node JsonViewer "Inputs" / "Outputs" headings that
    //    the legacy left-column inspector used. They live in
    //    StateEntrySheetBody now.
    expect(queryByText('Inputs')).toBeNull();
    expect(queryByText('Outputs')).toBeNull();
  });
});
