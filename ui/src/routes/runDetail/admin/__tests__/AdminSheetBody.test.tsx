/**
 * Tests for routes/runDetail/AdminSheetBody.tsx + its six section
 * components.
 *
 * The body is mounted with a synthetic ``manifest`` and an ``api``
 * mock so every section completes its initial fetch synchronously
 * (``vi.mock`` resolves all six list endpoints + ``getSpec`` with
 * canned shapes). We then assert:
 *
 *   1. all six sections render with the expected heading text;
 *   2. the Journal section is open by default (per
 *      ``defaultOpen={true}`` in JournalSection.tsx);
 *   3. the remaining sections are collapsed and toggle on click.
 */

import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import {
  cleanup,
  fireEvent,
  render,
  waitFor,
} from '@testing-library/preact';

vi.mock('../../../../lib/api', async () => {
  const sigPage = {
    items: [
      {
        id: 'sig-1',
        run_id: 'R',
        state_id: 'start',
        iteration_n: 1,
        brief_id: 'B',
        inputs_hash: 'aaaaaaaaaaaa',
        outputs_hash: 'bbbbbbbbbbbb',
        session_id: 'S',
        signature: 'cccccccccccc',
        verified: true,
        created_at: '2025-01-01T00:00:00Z',
      },
    ],
    page: 1,
    page_size: 200,
    total: 1,
    has_next: false,
    sort: '',
  };
  const journalPage = {
    items: [
      {
        id: 'jt-1',
        run_id: 'R',
        status: 'pending',
        staged_writes: [{ k: 'v' }],
        started_at: '2025-01-01T00:00:00Z',
        ready_at: null,
        finalised_at: null,
      },
      {
        // belongs to a different run -- must be filtered out client-side.
        id: 'jt-2',
        run_id: 'OTHER',
        status: 'finalised',
        staged_writes: [],
        started_at: '2025-01-01T00:00:00Z',
        ready_at: null,
        finalised_at: null,
      },
    ],
    page: 1,
    page_size: 200,
    total: 2,
    has_next: false,
    sort: '',
  };
  const locksPage = {
    items: [
      {
        run_id: 'R',
        holder_session_id: 'sess-1',
        acquired_at: '2025-01-01T00:00:00Z',
        expires_at: '2025-01-01T01:00:00Z',
      },
    ],
    page: 1,
    page_size: 200,
    total: 1,
    has_next: false,
    sort: '',
  };
  const toolsPage = {
    items: [
      {
        id: 'tc-1',
        run_id: 'R',
        producer_id: 'agent-foo',
        tool_name: 'Bash',
        args_redacted: { cmd: 'ls' },
        succeeded: true,
        created_at: '2025-01-01T00:00:00Z',
      },
    ],
    page: 1,
    page_size: 100,
    total: 1,
    has_next: false,
    sort: '',
  };
  const drift = {
    run_id: 'R',
    score: 0.42,
    signals: [
      {
        id: 'd-1',
        run_id: 'R',
        producer_id: 'p',
        signal_kind: 'tool_outside_allowlist',
        weight: 0.5,
        payload: {},
        created_at: '2025-01-01T00:00:00Z',
      },
    ],
  };
  const spec = {
    id: 'spec-1',
    slug: 'demo',
    version: 1,
    project_id: 'p',
    project_slug: null,
    fsm_hash: '',
    created_at: '2025-01-01T00:00:00Z',
    description: '',
    definition: {
      states: {
        start: { allowed_tools: ['Bash', 'Read'] },
      },
    },
  };
  class ApiErrorMock extends Error {}
  return {
    ApiError: ApiErrorMock,
    api: {
      listJournalTxns: vi.fn(async () => journalPage),
      listLocks: vi.fn(async () => locksPage),
      listToolCalls: vi.fn(async () => toolsPage),
      listDriftSignals: vi.fn(async () => drift),
      listCommitSignatures: vi.fn(async () => sigPage),
      getSpec: vi.fn(async () => spec),
    },
  };
});

import { AdminSheetBody } from '../../AdminSheetBody';
import type { RunManifest } from '../../../../lib/api';

const MANIFEST: RunManifest = {
  id: 'R',
  project_id: 'P',
  fsm_spec_id: 'spec-1',
  fsm_spec_hash: 'h',
  status: 'running',
  current_state: 'start',
  next_state: null,
  verdict: null,
  started_at: '2025-01-01T00:00:00Z',
  ended_at: null,
  last_update_at: '2025-01-01T00:00:00Z',
  paused_at: null,
  pause_reason: null,
  parent_run_id: null,
  resume_history: [],
  args: {},
  metadata: {},
  transitions_count: 0,
};

beforeEach(() => {
  cleanup();
});
afterEach(() => {
  cleanup();
});

describe('AdminSheetBody', () => {
  test('renders all six section headings', async () => {
    const { getByText } = render(
      <AdminSheetBody runId="R" manifest={MANIFEST} />,
    );
    expect(getByText('Journal')).toBeInTheDocument();
    expect(getByText('Lock')).toBeInTheDocument();
    expect(getByText('Drift')).toBeInTheDocument();
    expect(getByText('Commit signatures')).toBeInTheDocument();
    expect(getByText('Allowed tools (start)')).toBeInTheDocument();
    expect(getByText('Tool calls')).toBeInTheDocument();
  });

  test('Journal section is expanded by default and shows filtered txns', async () => {
    const { getByText, queryByText } = render(
      <AdminSheetBody runId="R" manifest={MANIFEST} />,
    );
    const header = getByText('Journal').closest('button')!;
    expect(header.getAttribute('aria-expanded')).toBe('true');
    // The "OTHER" run's txn must be filtered out client-side; only the
    // matching one renders.
    await waitFor(() => {
      expect(getByText('pending')).toBeInTheDocument();
    });
    expect(queryByText('finalised')).toBeNull();
  });

  test('Lock section collapsed by default, expand toggles aria-expanded', () => {
    const { getByText, getAllByRole } = render(
      <AdminSheetBody runId="R" manifest={MANIFEST} />,
    );
    const lockHeader = getByText('Lock').closest('button')!;
    expect(lockHeader.getAttribute('aria-expanded')).toBe('false');
    fireEvent.click(lockHeader);
    expect(lockHeader.getAttribute('aria-expanded')).toBe('true');
    fireEvent.click(lockHeader);
    expect(lockHeader.getAttribute('aria-expanded')).toBe('false');
    // sanity: there are at least six section headers in total.
    const buttons = getAllByRole('button').filter((b) =>
      b.hasAttribute('aria-expanded'),
    );
    expect(buttons.length).toBeGreaterThanOrEqual(6);
  });

  test('Drift / Signatures / AllowedTools / ToolCalls all toggle', async () => {
    const { getByText } = render(
      <AdminSheetBody runId="R" manifest={MANIFEST} />,
    );
    for (const title of [
      'Drift',
      'Commit signatures',
      'Allowed tools (start)',
      'Tool calls',
    ]) {
      const header = getByText(title).closest('button')!;
      expect(header.getAttribute('aria-expanded')).toBe('false');
      fireEvent.click(header);
      expect(header.getAttribute('aria-expanded')).toBe('true');
    }
    // After expanding AllowedTools the resolved spec allow-list renders
    // as <li> Pills inside the section's region. Bash also appears in
    // the ToolCalls section's tool_name column, so we scope the query
    // to the allow-list region by walking the heading button -> next
    // sibling (the role="region" panel).
    const allowedHeader = getByText('Allowed tools (start)').closest('button')!;
    const allowedRegion = allowedHeader.parentElement!.querySelector(
      '[role="region"]',
    )! as HTMLElement;
    await waitFor(() => {
      const items = Array.from(allowedRegion.querySelectorAll('li')).map(
        (li) => li.textContent?.trim(),
      );
      expect(items).toContain('Bash');
      expect(items).toContain('Read');
    });
  });
});
