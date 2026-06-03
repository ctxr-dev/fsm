/**
 * ``<RunEventTimeline>`` — vertical event log for a single run.
 *
 * Lifted out of ``runDetail.tsx`` during the PR 7 layout cutover so the
 * right column of the new 50/50 grid can render the timeline without
 * the route having to inline the ``eventToTimelineItem`` adapter, the
 * EmptyState branch, or the scroll wrapper. The timeline is the only
 * consumer of those helpers post-cutover.
 *
 * Visual contract:
 *
 *   - Fills the parent's height (``h-full min-h-0`` on the outer
 *     wrapper) so a flex / grid parent gets to dictate the size. The
 *     inner scroller is ``overflow-auto`` so a long event tape scrolls
 *     within the column rather than blowing out the page.
 *   - Newest event first (callers pre-sort; the timeline doesn't
 *     re-order so the SSE prepend behaviour from the route is
 *     preserved).
 *   - Renders an EmptyState when ``events`` is empty so the column
 *     never collapses to zero height.
 *
 * The colour-by-kind variant map lives here too because the route no
 * longer needs it — once the inline timeline was gone there was no
 * other call site.
 */

import type { JSX, VNode } from 'preact';
import { useMemo } from 'preact/hooks';

import { EmptyState } from './EmptyState';
import { JsonViewer } from './JsonViewer';
import { Timeline, type TimelineItem } from './Timeline';
import type { PillVariant } from './Pill';
import type { Event as FsmEvent } from '../lib/api';

export interface RunEventTimelineProps {
  /** Events already filtered by the route (newest first). */
  events: FsmEvent[];
  /** Optional className appended to the outer wrapper. */
  className?: string;
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

/** Render an FSM event as a :class:`TimelineItem`. */
function eventToTimelineItem(event: FsmEvent): TimelineItem {
  const variant = variantForEventKind(event.kind);
  const payload: VNode = (
    <JsonViewer
      value={event.payload}
      rootLabel="payload"
      mode="inline"
      maxInlineHeight="max-h-40"
      ariaLabel={`Payload of event ${event.id}`}
    />
  );
  return {
    id: event.id,
    timestamp: event.created_at,
    title: event.producer_id,
    kind: event.kind,
    variant,
    payload,
  };
}

export function RunEventTimeline({
  events,
  className = '',
}: RunEventTimelineProps): JSX.Element {
  const items = useMemo(() => events.map(eventToTimelineItem), [events]);

  const composed = ['flex flex-col h-full min-h-0', className]
    .filter(Boolean)
    .join(' ');

  if (items.length === 0) {
    return (
      <div class={composed} data-testid="run-event-timeline">
        <EmptyState
          title="No events yet"
          message="Events will appear here as the run produces them."
        />
      </div>
    );
  }

  return (
    <div class={composed} data-testid="run-event-timeline">
      <div class="flex-1 min-h-0 overflow-auto pr-1">
        <Timeline items={items} label="Run event timeline" />
      </div>
    </div>
  );
}

export default RunEventTimeline;
