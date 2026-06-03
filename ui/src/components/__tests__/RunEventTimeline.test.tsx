/**
 * Tests for ``RunEventTimeline`` — the freshness flash and entry-id
 * filter behaviour introduced in PR 6 of the /runs/:id redesign.
 *
 * The flash is asserted via the row's ``data-fresh`` attribute (the
 * CSS rule that consumes it lives in ``theme.css`` and is exercised by
 * the browser, not jsdom; the attribute is the contract).
 */

import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { act, cleanup, render } from '@testing-library/preact';

import { RunEventTimeline } from '../RunEventTimeline';
import type { Event as FsmEvent } from '../../lib/api';

function makeEvent(overrides: Partial<FsmEvent> = {}): FsmEvent {
  return {
    id: overrides.id ?? 'evt-1',
    run_id: overrides.run_id ?? 'run-1',
    kind: overrides.kind ?? 'state_entered',
    producer_id: overrides.producer_id ?? 'engine',
    payload: overrides.payload ?? {},
    created_at: overrides.created_at ?? '2025-01-01T00:00:00Z',
    seq: overrides.seq ?? 1,
  };
}

function rowsByEventId(container: Element | HTMLElement): Map<string, Element> {
  const out = new Map<string, Element>();
  for (const li of Array.from(
    container.querySelectorAll('li[data-event-id]'),
  )) {
    const id = li.getAttribute('data-event-id');
    if (id) out.set(id, li);
  }
  return out;
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe('RunEventTimeline (freshness flash)', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  test('initial render does not flash any of the seeded events', () => {
    const events = [
      makeEvent({ id: 'a' }),
      makeEvent({ id: 'b', created_at: '2025-01-01T00:00:01Z' }),
    ];
    const { container } = render(<RunEventTimeline events={events} />);
    const rows = rowsByEventId(container);
    expect(rows.size).toBe(2);
    for (const row of rows.values()) {
      expect(row.getAttribute('data-fresh')).toBe('false');
    }
  });

  test('a newly-prepended event row gets data-fresh="true"', () => {
    const initial = [makeEvent({ id: 'a' })];
    const { container, rerender } = render(
      <RunEventTimeline events={initial} />,
    );

    const grown = [makeEvent({ id: 'b', seq: 2 }), ...initial];
    act(() => {
      rerender(<RunEventTimeline events={grown} />);
    });

    const rows = rowsByEventId(container);
    expect(rows.get('b')?.getAttribute('data-fresh')).toBe('true');
    expect(rows.get('a')?.getAttribute('data-fresh')).toBe('false');
  });

  test('the data-fresh attribute clears ~2 s after the event arrives', () => {
    const initial = [makeEvent({ id: 'a' })];
    const { container, rerender } = render(
      <RunEventTimeline events={initial} />,
    );

    act(() => {
      rerender(
        <RunEventTimeline events={[makeEvent({ id: 'b', seq: 2 }), ...initial]} />,
      );
    });
    expect(rowsByEventId(container).get('b')?.getAttribute('data-fresh')).toBe(
      'true',
    );

    act(() => {
      vi.advanceTimersByTime(2100);
    });
    expect(rowsByEventId(container).get('b')?.getAttribute('data-fresh')).toBe(
      'false',
    );
  });

  test('entryIdFilter narrows the rendered events to matching ones', () => {
    const events = [
      makeEvent({ id: 'a', payload: { state_id: 'plan' } }),
      makeEvent({
        id: 'b',
        payload: { state_id: 'execute' },
        created_at: '2025-01-01T00:00:01Z',
      }),
      makeEvent({
        id: 'c',
        payload: { entry_id: 'plan' },
        created_at: '2025-01-01T00:00:02Z',
      }),
    ];
    const { container } = render(
      <RunEventTimeline events={events} entryIdFilter="plan" />,
    );
    const rows = rowsByEventId(container);
    expect(rows.has('a')).toBe(true);
    expect(rows.has('c')).toBe(true);
    expect(rows.has('b')).toBe(false);
  });

  test('empty list renders the EmptyState branch (no rows)', () => {
    const { container, getByText } = render(
      <RunEventTimeline events={[]} />,
    );
    expect(rowsByEventId(container).size).toBe(0);
    expect(getByText(/No events yet/i)).toBeInTheDocument();
  });
});
