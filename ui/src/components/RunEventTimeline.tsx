/**
 * ``<RunEventTimeline>`` — vertical event log for a single run.
 *
 * Lifted out of ``runDetail.tsx`` during the PR 7 layout cutover so the
 * right column of the new 50/50 grid can render the timeline without
 * the route having to inline the ``eventToTimelineItem`` adapter, the
 * EmptyState branch, or the scroll wrapper. The timeline is the only
 * consumer of those helpers post-cutover.
 *
 * PR 6 freshness flash
 * --------------------
 *
 * SSE-driven prepends arrive at the head of the ``events`` prop. Each
 * newly-arrived id gets an amber background flash for ~2 s so the
 * operator's eye lands on the new row instead of scanning the whole
 * tape looking for what changed. The flash is implemented as a
 * ``data-fresh="true"`` attribute on the row plus a CSS rule in
 * ``theme.css`` so the colour transition is GPU-driven and respects
 * the ``prefers-reduced-motion`` media query.
 *
 * Why we render the rows inline rather than via ``<Timeline>``: the
 * shared :class:`Timeline` primitive does not (yet) accept a per-row
 * data attribute. Re-implementing the small row markup here keeps the
 * shared component pure and lets the timeline own its flash mechanism
 * without leaking the SSE freshness concern into every other Timeline
 * caller.
 *
 * The ``entryIdFilter`` prop narrows the tape to events whose
 * ``payload.state_id`` (the most common reference) or ``entry_id``
 * matches a single state entry — used by panels that scope a
 * per-entry slice of the run (eg. the StateEntrySheet's "Events" tab,
 * if it ever wires the timeline directly).
 */

import type { JSX, VNode } from 'preact';
import { useEffect, useMemo, useRef, useState } from 'preact/hooks';

import { EmptyState } from './EmptyState';
import { JsonViewer } from './JsonViewer';
import { Pill, type PillVariant } from './Pill';
import type { Event as FsmEvent } from '../lib/api';

export interface RunEventTimelineProps {
  /** Events already filtered by the route (newest first). */
  events: FsmEvent[];
  /** Optional className appended to the outer wrapper. */
  className?: string;
  /**
   * Optional entry-id filter. When set, only events whose payload
   * references this entry (via ``entry_id`` or ``state_id``) render.
   */
  entryIdFilter?: string;
}

/** Pick a pill colour for an event row in the timeline. */
function variantForEventKind(kind: string): PillVariant {
  const k = kind.toLowerCase();
  if (k.includes('error') || k.includes('fault') || k.includes('abort')) {
    return 'danger';
  }
  if (k.includes('warn') || k.includes('retry') || k.includes('pause')) {
    return 'warning';
  }
  if (k.includes('complete') || k.includes('success') || k.includes('commit')) {
    return 'success';
  }
  if (k.includes('state') || k.includes('transition') || k.includes('enter')) {
    return 'info';
  }
  return 'neutral';
}

/** Map a variant to its dot bg class (Timeline's colour scheme, kept in sync). */
function dotClass(variant: PillVariant): string {
  if (variant === 'success') return 'bg-emerald-500';
  if (variant === 'warning') return 'bg-amber-500';
  if (variant === 'danger') return 'bg-red-500';
  if (variant === 'info') return 'bg-slate-400';
  return 'bg-slate-300 dark:bg-slate-600';
}

/** Format an ISO timestamp as locale ``YYYY-MM-DD HH:MM:SS``. */
function formatTimestamp(iso: string): string {
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleString(undefined, {
      hour12: false,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    });
  } catch {
    return iso;
  }
}

/** Render the payload as an inline JsonViewer (lazy / collapsed). */
function payloadFor(event: FsmEvent): VNode {
  return (
    <JsonViewer
      value={event.payload}
      rootLabel="payload"
      mode="inline"
      maxInlineHeight="max-h-40"
      ariaLabel={`Payload of event ${event.id}`}
    />
  );
}

/** Predicate: does the event reference the given entry id? */
function eventMatchesEntry(event: FsmEvent, entryId: string): boolean {
  const p = (event.payload ?? {}) as Record<string, unknown>;
  if (typeof p.entry_id === 'string' && p.entry_id === entryId) return true;
  if (typeof p.state_id === 'string' && p.state_id === entryId) return true;
  return false;
}

const FRESHNESS_MS = 2000;

export function RunEventTimeline({
  events,
  className = '',
  entryIdFilter,
}: RunEventTimelineProps): JSX.Element {
  const filtered = useMemo(() => {
    if (!entryIdFilter) return events;
    return events.filter((e) => eventMatchesEntry(e, entryIdFilter));
  }, [events, entryIdFilter]);

  // --- Freshness tracking -------------------------------------------------
  // Track ids we have already "seen" so a remount with a preloaded list
  // does not flash every row amber on first paint. Only ids that appear
  // AFTER the first render (ie. SSE prepends) get the flash.
  const seenIdsRef = useRef<Set<string>>(new Set());
  const initialisedRef = useRef<boolean>(false);
  const timeoutsRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(
    new Map(),
  );
  const [freshIds, setFreshIds] = useState<Set<string>>(new Set());

  useEffect(() => {
    const seen = seenIdsRef.current;
    if (!initialisedRef.current) {
      // Seed with the initial events so the first paint is calm.
      for (const e of events) seen.add(e.id);
      initialisedRef.current = true;
      return;
    }
    const newIds: string[] = [];
    for (const e of events) {
      if (!seen.has(e.id)) {
        seen.add(e.id);
        newIds.push(e.id);
      }
    }
    if (newIds.length === 0) return;
    setFreshIds((prev) => {
      const next = new Set(prev);
      for (const id of newIds) next.add(id);
      return next;
    });
    for (const id of newIds) {
      const existing = timeoutsRef.current.get(id);
      if (existing != null) clearTimeout(existing);
      const handle = setTimeout(() => {
        timeoutsRef.current.delete(id);
        setFreshIds((prev) => {
          if (!prev.has(id)) return prev;
          const next = new Set(prev);
          next.delete(id);
          return next;
        });
      }, FRESHNESS_MS);
      timeoutsRef.current.set(id, handle);
    }
  }, [events]);

  // Clear pending flash timers on unmount so we don't fire setState into
  // a torn-down tree (eg. when the route is navigated away mid-flash).
  useEffect(() => {
    return () => {
      for (const h of timeoutsRef.current.values()) clearTimeout(h);
      timeoutsRef.current.clear();
    };
  }, []);

  const composed = ['flex flex-col h-full min-h-0', className]
    .filter(Boolean)
    .join(' ');

  if (filtered.length === 0) {
    return (
      <div class={composed} data-testid="run-event-timeline">
        <EmptyState
          title="No events yet"
          message={
            entryIdFilter
              ? 'No events reference this state entry.'
              : 'Events will appear here as the run produces them.'
          }
        />
      </div>
    );
  }

  return (
    <div class={composed} data-testid="run-event-timeline">
      {/* role="log" + aria-live="polite" + aria-relevant="additions" so
          screen readers announce SSE-prepended rows as they land without
          re-reading the existing tape. The live region wraps the scroll
          container rather than the <ol> itself so the list keeps its
          native list semantics (an aria-role on the <ol> would strip the
          implicit list role and orphan each <li>). Each <li> exposes a
          visually-hidden announceable summary (timestamp + kind +
          producer) so the announcement is concise and skips the
          JsonViewer payload, which would otherwise be read aloud as a
          long unstructured blob on every prepend. */}
      <div
        class="flex-1 min-h-0 overflow-auto pr-1"
        role="log"
        aria-live="polite"
        aria-relevant="additions"
        aria-label="Run event timeline"
      >
        <ol class="relative" aria-label="Run event timeline">
          {filtered.map((event, index) => {
            const variant = variantForEventKind(event.kind);
            const isLast = index === filtered.length - 1;
            const isFresh = freshIds.has(event.id);
            const announceSummary =
              `${formatTimestamp(event.created_at)} ${event.kind} ${event.producer_id}`.trim();
            return (
              <li
                key={event.id}
                data-event-id={event.id}
                data-fresh={isFresh ? 'true' : 'false'}
                class="relative flex gap-3 pb-4 last:pb-0 rounded-md"
              >
                <span class="sr-only" data-testid="event-announce-summary">
                  {announceSummary}
                </span>
                {!isLast ? (
                  <span
                    aria-hidden="true"
                    class="absolute left-[7px] top-4 bottom-0 w-px bg-slate-200 dark:bg-slate-700"
                  />
                ) : null}
                <span
                  aria-hidden="true"
                  class={[
                    'mt-1.5 h-[14px] w-[14px] rounded-full ring-2 ring-white dark:ring-slate-900 shrink-0',
                    dotClass(variant),
                  ].join(' ')}
                />
                <div class="flex-1 min-w-0">
                  <div class="flex flex-wrap items-baseline gap-2">
                    <time
                      dateTime={event.created_at}
                      class="font-mono text-xs text-slate-500 dark:text-slate-400"
                    >
                      {formatTimestamp(event.created_at)}
                    </time>
                    <Pill variant={variant} size="sm">
                      {event.kind}
                    </Pill>
                    <span class="text-sm font-medium text-slate-900 dark:text-slate-100">
                      {event.producer_id}
                    </span>
                  </div>
                  <div class="mt-1 text-sm text-slate-700 dark:text-slate-300">
                    {payloadFor(event)}
                  </div>
                </div>
              </li>
            );
          })}
        </ol>
      </div>
    </div>
  );
}

export default RunEventTimeline;
