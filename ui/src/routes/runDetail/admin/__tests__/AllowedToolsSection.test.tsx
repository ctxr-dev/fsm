/**
 * Regression tests for AllowedToolsSection.tsx (finding #6).
 *
 * The pre-fix implementation cast ``spec.definition`` as
 * ``Record<string, unknown>`` and indexed ``definition.states`` by
 * ``stateId``. That works ONLY when the wire shape is a keyed dict —
 * but the canonical FsmSpec serialiser (matched by ``SpecStateShape``
 * in ``specGraph.ts``) emits ``states`` as an ARRAY of
 * ``{ id, ... }`` objects. The cast hid that mismatch and the section
 * silently fell into the "Not surfaced" EmptyState branch for every
 * real spec.
 *
 * The fix:
 *   1. Types ``definition`` against its real shape
 *      (``{ states?: SpecState[] | Record<string, SpecState> }``);
 *   2. Uses ``Array.prototype.find`` against ``state.id`` for the array
 *      path;
 *   3. Keeps a defensive runtime guard for the keyed-dict form so any
 *      future serialiser variant still works.
 */

import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { cleanup, render, waitFor } from '@testing-library/preact';

vi.mock('../../../../lib/api', () => {
  class ApiErrorMock extends Error {}
  return {
    ApiError: ApiErrorMock,
    api: {
      // populated per-test via mockResolvedValue
      getSpec: vi.fn(),
    },
  };
});

import { AllowedToolsSection } from '../AllowedToolsSection';
import { api } from '../../../../lib/api';
import type { RunManifest } from '../../../../lib/api';

const MANIFEST: RunManifest = {
  id: 'R',
  project_id: 'P',
  fsm_spec_id: 'spec-1',
  fsm_spec_hash: 'h',
  status: 'running',
  current_state: 'plan_phase',
  next_state: null,
  verdict: null,
  started_at: '2025-01-01T00:00:00Z',
  ended_at: null,
  last_update_at: '2025-01-01T00:00:00Z',
  paused_at: null,
  pause_reason: null,
  parent_run_id: null,
  transitions_count: 0,
} as unknown as RunManifest;

beforeEach(() => {
  vi.clearAllMocks();
});
afterEach(() => cleanup());

describe('AllowedToolsSection (regression #6)', () => {
  test('renders the allow-list when definition.states is an ARRAY (canonical FsmSpec.model_dump shape)', async () => {
    // Pre-fix, this exact spec shape would have fallen through to the
    // "Not surfaced by the current state's spec entry" EmptyState
    // because the code indexed an array by a string key. The fix uses
    // `.find(s => s.id === stateId)` so the lookup actually resolves.
    (api.getSpec as ReturnType<typeof vi.fn>).mockResolvedValue({
      id: 'spec-1',
      project_id: 'p',
      project_slug: 'p',
      slug: 'demo',
      version: 1,
      hash: 'h',
      registered_at: '2025-01-01T00:00:00Z',
      definition: {
        entry: 'plan_phase',
        states: [
          { id: 'plan_phase', kind: 'worker', allowed_tools: ['Bash', 'Read'] },
          { id: 'execute_phase', kind: 'worker', allowed_tools: ['Edit'] },
        ],
      },
    });
    const { container, queryByText } = render(
      <AllowedToolsSection manifest={MANIFEST} />,
    );
    // Expand the section so the body renders (it ships defaultOpen=false).
    const header = container.querySelector(
      'button[aria-controls]',
    ) as HTMLButtonElement | null;
    if (header && header.getAttribute('aria-expanded') === 'false') {
      header.click();
    }
    // Wait for the spec fetch + render.
    await waitFor(() => {
      expect(queryByText('Bash')).toBeInTheDocument();
    });
    expect(queryByText('Read')).toBeInTheDocument();
    // The "other" state's allowed tool MUST NOT appear — the lookup is
    // scoped to current_state ('plan_phase').
    expect(queryByText('Edit')).toBeNull();
    // The pre-fix fallback message MUST be absent — the array path now
    // resolves and the surfaced list renders instead.
    expect(
      queryByText("Not surfaced by the current state's spec entry."),
    ).toBeNull();
  });

  test('still tolerates the keyed-dict states shape (defensive guard)', async () => {
    // Some hand-written fixtures (and the existing AdminSheetBody test)
    // use a keyed-dict form. Keep it working so the fix is purely
    // additive — no consumer regresses.
    (api.getSpec as ReturnType<typeof vi.fn>).mockResolvedValue({
      id: 'spec-1',
      project_id: 'p',
      project_slug: 'p',
      slug: 'demo',
      version: 1,
      hash: 'h',
      registered_at: '2025-01-01T00:00:00Z',
      definition: {
        states: {
          plan_phase: { allowed_tools: ['Grep'] },
        },
      },
    });
    const { container, queryByText } = render(
      <AllowedToolsSection manifest={MANIFEST} />,
    );
    const header = container.querySelector(
      'button[aria-controls]',
    ) as HTMLButtonElement | null;
    if (header && header.getAttribute('aria-expanded') === 'false') {
      header.click();
    }
    await waitFor(() => {
      expect(queryByText('Grep')).toBeInTheDocument();
    });
  });
});
