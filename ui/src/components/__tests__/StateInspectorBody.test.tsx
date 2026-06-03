/**
 * Smoke test for components/StateInspectorBody.tsx.
 *
 * Renders a sample worker state and asserts the inspector surfaces
 * the state id and kind in the top-of-body metadata table.
 */

import { afterEach, describe, expect, test } from 'vitest';
import { cleanup, render } from '@testing-library/preact';

import { StateInspectorBody } from '../StateInspectorBody';

afterEach(() => {
  cleanup();
});

describe('StateInspectorBody', () => {
  test('renders state id and kind in the metadata table', () => {
    const state = {
      id: 'plan_phase',
      kind: 'worker',
      worker: { role: 'planner' },
    };
    // Body renders the id+kind both in the top-of-body KeyValueTable
    // and inside the raw state JSON viewer at the bottom, so the
    // value strings appear multiple times. Assert via getAllByText
    // that each shows up at least once.
    const { getByText, getAllByText } = render(
      <StateInspectorBody state={state} isEntry={false} />,
    );
    expect(getByText('id')).toBeInTheDocument();
    expect(getByText('kind')).toBeInTheDocument();
    expect(getAllByText('plan_phase').length).toBeGreaterThan(0);
    expect(getAllByText('worker').length).toBeGreaterThan(0);
  });
});
