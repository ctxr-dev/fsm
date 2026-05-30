import type { JSX, VNode } from 'preact';
import { Pill, type PillVariant } from './Pill';

export interface TimelineItem {
  /** Stable id used as the row key. */
  id: string;
  /** ISO 8601 timestamp string. Rendered as locale time. */
  timestamp: string;
  /** Short label for the event row. */
  title: string;
  /** Optional event kind (used to colour the pill). */
  kind?: string;
  /** Map kind -> pill variant. Defaults to neutral if not provided. */
  variant?: PillVariant;
  /** Optional structured payload rendered below the title. */
  payload?: VNode | string;
}

export interface TimelineProps {
  items: TimelineItem[];
  /** Optional label for the region (screen-reader use). */
  label?: string;
  className?: string;
}

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

/**
 * Timeline — vertical event log. Each item gets a coloured dot whose hue
 * matches its `variant`, then a row with timestamp, kind pill, title, and
 * optional payload (usually a small <pre> or table).
 */
export function Timeline({
  items,
  label = 'Event timeline',
  className = '',
}: TimelineProps): JSX.Element {
  const composed = ['relative', className].filter(Boolean).join(' ');
  return (
    <ol class={composed} aria-label={label}>
      {items.map((item, index) => {
        const variant = item.variant ?? 'neutral';
        const isLast = index === items.length - 1;
        return (
          <li key={item.id} class="relative flex gap-3 pb-4 last:pb-0">
            {/* Connector line */}
            {!isLast ? (
              <span
                aria-hidden="true"
                class="absolute left-[7px] top-4 bottom-0 w-px bg-slate-200 dark:bg-slate-700"
              />
            ) : null}
            {/* Dot */}
            <span
              aria-hidden="true"
              class={[
                'mt-1.5 h-[14px] w-[14px] rounded-full ring-2 ring-white dark:ring-slate-900 shrink-0',
                variant === 'success'
                  ? 'bg-emerald-500'
                  : variant === 'warning'
                  ? 'bg-amber-500'
                  : variant === 'danger'
                  ? 'bg-red-500'
                  : variant === 'info'
                  ? 'bg-slate-400'
                  : 'bg-slate-300 dark:bg-slate-600',
              ].join(' ')}
            />
            {/* Body */}
            <div class="flex-1 min-w-0">
              <div class="flex flex-wrap items-baseline gap-2">
                <time
                  dateTime={item.timestamp}
                  class="font-mono text-xs text-slate-500 dark:text-slate-400"
                >
                  {formatTimestamp(item.timestamp)}
                </time>
                {item.kind ? (
                  <Pill variant={variant} size="sm">
                    {item.kind}
                  </Pill>
                ) : null}
                <span class="text-sm font-medium text-slate-900 dark:text-slate-100">
                  {item.title}
                </span>
              </div>
              {item.payload ? (
                <div class="mt-1 text-sm text-slate-700 dark:text-slate-300">
                  {item.payload}
                </div>
              ) : null}
            </div>
          </li>
        );
      })}
    </ol>
  );
}

export default Timeline;
