/**
 * Tests for routes/runDetail/StateEntrySheetBody.tsx.
 *
 * Coverage:
 *   - The three tabs (Run values / Spec definition / Events for this
 *     state) all render and the operator can switch between them via
 *     the role="tab" buttons.
 *   - Tab 2 mounts StateInspectorBody (assert the inspector's metadata
 *     "id" / "kind" row labels show up once the tab is active).
 *   - Tab 3 filters the events by entry_id (an event whose payload
 *     references the entry id appears; an unrelated event does not).
 */

import { afterEach, describe, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render } from '@testing-library/preact';

import { StateEntrySheetBody } from '../StateEntrySheetBody';
import type { Event as FsmEvent, SpecDetail, StateNode } from '../../../lib/api';

afterEach(() => cleanup());

const ENTRY_ID = 'entry-abc';
const STATE_ID = 'plan_phase';

const STATE_TREE: StateNode = {
  entry_id: ENTRY_ID,
  state_id: STATE_ID,
  entry_seq: 1,
  entered_at: '2025-01-01T00:00:00Z',
  exited_at: null,
  status: 'entered',
  inputs: { x: 1 },
  outputs: { y: 2 },
  iteration_n: 0,
  children: [],
};

const SPEC: SpecDetail = {
  id: 'spec-1',
  project_id: 'p',
  project_slug: 'p',
  slug: 'demo',
  version: 1,
  hash: 'h',
  registered_at: '2025-01-01T00:00:00Z',
  definition: {
    entry: STATE_ID,
    states: [
      {
        id: STATE_ID,
        kind: 'worker',
        worker: { role: 'planner' },
      },
      { id: 'other', kind: 'state' },
    ],
  },
};

const RELATED_EVENT: FsmEvent = {
  id: 'evt-1',
  run_id: 'R',
  kind: 'state_entered',
  producer_id: 'engine',
  payload: { state_entry_id: ENTRY_ID },
  created_at: '2025-01-01T00:00:01Z',
  seq: 1,
};

const COMMIT_EVENT: FsmEvent = {
  id: 'evt-2',
  run_id: 'R',
  kind: 'worker_committed',
  producer_id: 'engine',
  payload: {
    state_entry_id: ENTRY_ID,
    signature: 'sig-deadbeef',
    brief_id: 'brief-xyz',
  },
  created_at: '2025-01-01T00:00:02Z',
  seq: 2,
};

const UNRELATED_EVENT: FsmEvent = {
  id: 'evt-3',
  run_id: 'R',
  kind: 'state_entered',
  producer_id: 'engine',
  payload: { state_entry_id: 'some-other-entry' },
  created_at: '2025-01-01T00:00:03Z',
  seq: 3,
};

describe('StateEntrySheetBody', () => {
  test('renders all three tabs with the expected labels', () => {
    const { getByRole } = render(
      <StateEntrySheetBody
        entryId={ENTRY_ID}
        runId="R"
        stateTree={STATE_TREE}
        spec={SPEC}
        events={[RELATED_EVENT, COMMIT_EVENT, UNRELATED_EVENT]}
      />,
    );
    expect(getByRole('tab', { name: /Run values/ })).toBeInTheDocument();
    expect(getByRole('tab', { name: /Spec definition/ })).toBeInTheDocument();
    expect(getByRole('tab', { name: /Events for this state/ })).toBeInTheDocument();
  });

  test('tab 1 (default) shows the run-recorded metadata for this entry', () => {
    const { getAllByText, getByTestId } = render(
      <StateEntrySheetBody
        entryId={ENTRY_ID}
        runId="R"
        stateTree={STATE_TREE}
        spec={SPEC}
        events={[RELATED_EVENT, COMMIT_EVENT, UNRELATED_EVENT]}
      />,
    );
    expect(getByTestId('state-entry-run-panel')).toBeInTheDocument();
    expect(getAllByText('state_id').length).toBeGreaterThan(0);
    expect(getAllByText('entered_at').length).toBeGreaterThan(0);
    expect(getAllByText('signature').length).toBeGreaterThan(0);
  });

  test('tab 2 mounts StateInspectorBody (id / kind metadata rows)', () => {
    const { getByRole, getByText } = render(
      <StateEntrySheetBody
        entryId={ENTRY_ID}
        runId="R"
        stateTree={STATE_TREE}
        spec={SPEC}
        events={[]}
      />,
    );
    fireEvent.click(getByRole('tab', { name: /Spec definition/ }));
    // StateInspectorBody renders these two row labels at the top of its
    // metadata table; they are NOT rendered by the run-values tab so
    // their presence is a reliable proof the inspector mounted.
    expect(getByText('id')).toBeInTheDocument();
    expect(getByText('kind')).toBeInTheDocument();
    // The inspector also surfaces the spec-side worker.role row for
    // states with a worker block.
    expect(getByText('worker.role')).toBeInTheDocument();
  });

  test('tab 3 filters the event list by entry_id', () => {
    const { getByRole, getByTestId, queryByText } = render(
      <StateEntrySheetBody
        entryId={ENTRY_ID}
        runId="R"
        stateTree={STATE_TREE}
        spec={SPEC}
        events={[RELATED_EVENT, COMMIT_EVENT, UNRELATED_EVENT]}
      />,
    );
    fireEvent.click(getByRole('tab', { name: /Events for this state/ }));
    const eventsPanel = getByTestId('state-entry-events-panel');
    // Both related events should render — their kinds appear inside the
    // timeline as Pill text. The unrelated event's id is unique so we
    // can prove it does NOT render anywhere in the panel.
    expect(eventsPanel.textContent).toContain('state_entered');
    expect(eventsPanel.textContent).toContain('worker_committed');
    // No row from the unrelated event id should appear.
    expect(queryByText(/some-other-entry/)).toBeNull();
  });

  test('renders an EmptyState when the entry id is not in the tree', () => {
    const { getByText } = render(
      <StateEntrySheetBody
        entryId="unknown"
        runId="R"
        stateTree={STATE_TREE}
        spec={SPEC}
        events={[]}
      />,
    );
    expect(getByText('Entry not in tree')).toBeInTheDocument();
  });

  test('regression #5: iteration-chip buttons keep their native button role', () => {
    // Pre-fix, each iteration chip was rendered as
    //   <button role="listitem">…</button>
    // putting an ARIA role on a native interactive element replaces its
    // implicit role. Screen readers announced each chip as "list item N"
    // instead of "button N", and the chip lost the standard button
    // semantics keyboard-and-AT users rely on (Enter / Space activation,
    // role-based queries in tests).
    //
    // The fix wraps each chip in a <li> so the strip keeps list
    // semantics for AT users without overriding the button's role.
    const TREE: StateNode = {
      entry_id: 'iter-1',
      state_id: 'loop_phase',
      entry_seq: 1,
      entered_at: '2025-01-01T00:00:00Z',
      exited_at: '2025-01-01T00:00:01Z',
      status: 'exited',
      inputs: {},
      outputs: {},
      iteration_n: 1,
      children: [
        {
          entry_id: 'iter-2',
          state_id: 'loop_phase',
          entry_seq: 2,
          entered_at: '2025-01-01T00:00:02Z',
          exited_at: '2025-01-01T00:00:03Z',
          status: 'exited',
          inputs: {},
          outputs: {},
          iteration_n: 2,
          children: [],
        },
      ],
    };
    const { getAllByRole, getByTestId } = render(
      <StateEntrySheetBody
        entryId="iter-1"
        runId="R"
        stateTree={TREE}
        spec={null}
        events={[]}
      />,
    );
    // The chip strip itself is in the document — proves we're rendering
    // the iterations path the regression covers.
    expect(getByTestId('iterations-chip-strip')).toBeInTheDocument();
    // Each chip is queryable BY ROLE='button'. Pre-fix this was
    // impossible because the role had been overridden to 'listitem' and
    // testing-library would not match it as a button.
    const chipButtons = getAllByRole('button').filter(
      (el) => el.getAttribute('data-testid') === 'iterations-chip',
    );
    expect(chipButtons.length).toBe(2);
    // Each chip is a native <button> element — not a <div role="button">
    // or any other surrogate.
    for (const chip of chipButtons) {
      expect(chip.tagName).toBe('BUTTON');
      expect(chip.getAttribute('role')).toBeNull();
    }
    // The list semantics live on the wrapping <ul>/<li> so AT users
    // still hear "list of N iterations".
    const list = getByTestId('iterations-chip-strip');
    expect(list.tagName).toBe('UL');
    const listItems = list.querySelectorAll('li');
    expect(listItems.length).toBe(2);
  });

  test('regression #5: clicking a sibling iteration chip fires onSelectIteration', () => {
    // The native button keeps native click semantics; testing-library
    // fireEvent.click on the chip should hit the onSelectIteration
    // handler with the chip's entry_id.
    const TREE: StateNode = {
      entry_id: 'iter-1',
      state_id: 'loop_phase',
      entry_seq: 1,
      entered_at: '2025-01-01T00:00:00Z',
      exited_at: '2025-01-01T00:00:01Z',
      status: 'exited',
      inputs: {},
      outputs: {},
      iteration_n: 1,
      children: [
        {
          entry_id: 'iter-2',
          state_id: 'loop_phase',
          entry_seq: 2,
          entered_at: '2025-01-01T00:00:02Z',
          exited_at: '2025-01-01T00:00:03Z',
          status: 'exited',
          inputs: {},
          outputs: {},
          iteration_n: 2,
          children: [],
        },
      ],
    };
    const onSelect = vi.fn();
    const { getAllByRole } = render(
      <StateEntrySheetBody
        entryId="iter-1"
        runId="R"
        stateTree={TREE}
        spec={null}
        events={[]}
        onSelectIteration={onSelect}
      />,
    );
    const chipButtons = getAllByRole('button').filter(
      (el) => el.getAttribute('data-testid') === 'iterations-chip',
    );
    // Find the inactive chip (entry_id !== iter-1) and click it.
    const inactive = chipButtons.find(
      (b) => b.getAttribute('data-entry-id') === 'iter-2',
    );
    expect(inactive).toBeDefined();
    fireEvent.click(inactive!);
    expect(onSelect).toHaveBeenCalledWith('iter-2');
  });
});
