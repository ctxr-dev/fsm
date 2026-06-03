/**
 * Tests for routes/runDetail/EdgeSheetBody.tsx.
 *
 * Coverage:
 *   - "Not taken in this run" branch: a spec edge between two states
 *     that the run never traversed (no parent->child link in the tree
 *     and no transition_taken event) renders the EmptyState message.
 *   - "Taken" branch: the same edge with the run's state tree
 *     containing the parent/child link AND a transition event present
 *     renders the event's when_taken_at row and a "taken" pill.
 */

import { afterEach, describe, expect, test } from 'vitest';
import { cleanup, render } from '@testing-library/preact';

import { EdgeSheetBody } from '../EdgeSheetBody';
import type { Event as FsmEvent, SpecDetail, StateNode } from '../../../lib/api';

afterEach(() => cleanup());

const FROM = 'plan_phase';
const TO = 'execute_phase';

const SPEC: SpecDetail = {
  id: 'spec-1',
  project_id: 'p',
  project_slug: 'p',
  slug: 'demo',
  version: 1,
  hash: 'h',
  registered_at: '2025-01-01T00:00:00Z',
  definition: {
    entry: FROM,
    states: [
      {
        id: FROM,
        kind: 'worker',
        transitions: [
          // A non-trivial predicate so the EdgeSheetBody renders the
          // predicate text via the tokenised highlighter.
          { to: TO, when: "tier == 'trivial' AND len(risk_signals) == 0" },
        ],
      },
      { id: TO, kind: 'state' },
    ],
  },
};

const TREE_NOT_TAKEN: StateNode = {
  entry_id: 'entry-from',
  state_id: FROM,
  entry_seq: 1,
  entered_at: '2025-01-01T00:00:00Z',
  exited_at: null,
  status: 'entered',
  inputs: {},
  outputs: {},
  iteration_n: null,
  // No children -> no parent/child link -> edge not taken via tree.
  children: [],
};

const TREE_TAKEN: StateNode = {
  entry_id: 'entry-from',
  state_id: FROM,
  entry_seq: 1,
  entered_at: '2025-01-01T00:00:00Z',
  exited_at: '2025-01-01T00:00:02Z',
  status: 'exited',
  inputs: {},
  outputs: {},
  iteration_n: null,
  children: [
    {
      entry_id: 'entry-to',
      state_id: TO,
      entry_seq: 2,
      entered_at: '2025-01-01T00:00:02Z',
      exited_at: null,
      status: 'entered',
      inputs: {},
      outputs: {},
      iteration_n: null,
      children: [],
    },
  ],
};

const TRANSITION_EVENT: FsmEvent = {
  id: 'evt-tt',
  run_id: 'R',
  kind: 'transition_taken',
  producer_id: 'engine',
  payload: {
    from: FROM,
    to: TO,
    when_taken_at: '2025-01-01T00:00:02Z',
  },
  created_at: '2025-01-01T00:00:02Z',
  seq: 5,
};

describe('EdgeSheetBody', () => {
  test('renders "Not taken in this run" when the edge was never traversed', () => {
    const { getByText, getByTestId } = render(
      <EdgeSheetBody
        fromStateId={FROM}
        toStateId={TO}
        runId="R"
        stateTree={TREE_NOT_TAKEN}
        spec={SPEC}
        events={[]}
      />,
    );
    expect(getByTestId('edge-sheet-body')).toBeInTheDocument();
    expect(getByText('Not taken in this run')).toBeInTheDocument();
    expect(getByText('not taken')).toBeInTheDocument();
    // The predicate text from the spec is still rendered (the section
    // shows the spec-declared guard even when the edge never fired).
    // Tokens render via spans; assert on a fragment that appears in
    // the predicate so we don't depend on the exact token splitting.
    expect(getByText(/risk_signals/)).toBeInTheDocument();
  });

  test('renders the taken case with the transition event metadata', () => {
    const { getByText, queryByText } = render(
      <EdgeSheetBody
        fromStateId={FROM}
        toStateId={TO}
        runId="R"
        stateTree={TREE_TAKEN}
        spec={SPEC}
        events={[TRANSITION_EVENT]}
      />,
    );
    expect(getByText('taken')).toBeInTheDocument();
    expect(getByText('when_taken_at')).toBeInTheDocument();
    expect(getByText('transition_taken')).toBeInTheDocument();
    // The not-taken EmptyState must NOT render in the taken case.
    expect(queryByText('Not taken in this run')).toBeNull();
  });

  test('renders predicate text via tokenised highlighter spans', () => {
    const { container } = render(
      <EdgeSheetBody
        fromStateId={FROM}
        toStateId={TO}
        runId="R"
        stateTree={TREE_NOT_TAKEN}
        spec={SPEC}
        events={[]}
      />,
    );
    // The tokenised predicate code block carries the amber-pill
    // background classes; assert that the predicate <code> element
    // exists and contains the predicate fragments.
    const code = container.querySelector('code[title*="risk_signals"]');
    expect(code).not.toBeNull();
    expect(code?.textContent).toContain('tier');
    expect(code?.textContent).toContain('risk_signals');
  });
});
