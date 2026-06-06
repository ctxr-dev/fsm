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

  test('regression #2: timeline exposes a polite log live region for SSE-driven additions', () => {
    // Pre-fix, the <ol> carried only `aria-label="Run event timeline"`
    // and SSE-prepended rows landed silently — screen readers gave the
    // operator no audible cue that the tape grew. The fix surfaces a
    // log live region (role="log" + aria-live="polite" +
    // aria-relevant="additions") around the timeline so AT users hear
    // each new event as it arrives. The region wraps the scroll
    // container so the <ol> keeps its native list semantics (an aria
    // role on the <ol> would strip the implicit list role and orphan
    // every <li>).
    const events = [
      makeEvent({ id: 'a' }),
      makeEvent({ id: 'b', created_at: '2025-01-01T00:00:01Z' }),
    ];
    const { container } = render(<RunEventTimeline events={events} />);
    const liveRegion = container.querySelector('[role="log"]');
    expect(liveRegion).not.toBeNull();
    expect(liveRegion!.getAttribute('aria-live')).toBe('polite');
    expect(liveRegion!.getAttribute('aria-relevant')).toBe('additions');
    // The <ol> still renders inside the live region so list semantics
    // survive for the row navigation shortcuts AT users rely on.
    expect(liveRegion!.querySelector('ol')).not.toBeNull();
  });

  test('regression #2: each row carries a concise announceable summary (timestamp + kind + producer)', () => {
    // The announceable summary is what the live region's polite poke
    // reads aloud; we deliberately keep it short and skip the JsonViewer
    // payload (a long unstructured blob would otherwise be read on every
    // SSE prepend). The summary lives in a `.sr-only` span so sighted
    // users see the existing visual row unchanged.
    const events = [
      makeEvent({
        id: 'a',
        kind: 'state_entered',
        producer_id: 'engine',
        created_at: '2025-01-01T00:00:00Z',
      }),
    ];
    const { container } = render(<RunEventTimeline events={events} />);
    const summary = container.querySelector(
      'li[data-event-id="a"] [data-testid="event-announce-summary"]',
    );
    expect(summary).not.toBeNull();
    const text = summary!.textContent ?? '';
    expect(text).toContain('state_entered');
    expect(text).toContain('engine');
    // The JsonViewer payload markup MUST NOT be inside the summary so AT
    // users don't hear the full payload on every prepend.
    expect(summary!.querySelector('button')).toBeNull();
    expect(summary!.querySelector('pre')).toBeNull();
  });
});
