/**
 * FilterChips — chip row showing active filters with remove buttons.
 *
 * Pure render — URL coordination is the route's job. Pages wire it
 * to the relevant signal state and pass remove callbacks. The chip
 * row sits directly under the page header in Run Detail v2 (W18d).
 *
 * Empty state: renders nothing when chips array is empty (zero
 * height, no border).
 */

import type { JSX } from 'preact';
import { Pill } from './Pill';
import { Button } from './Button';

export interface FilterChip {
  id: string;
  kind: string; // e.g. 'state' | 'event-kind' | 'producer'
  label: string;
  removable?: boolean;
}

export interface FilterChipsProps {
  chips: readonly FilterChip[];
  onRemove: (chip: FilterChip) => void;
  onClear?: () => void;
  ariaLabel?: string;
  className?: string;
}

export function FilterChips({
  chips,
  onRemove,
  onClear,
  ariaLabel = 'Active filters',
  className,
}: FilterChipsProps): JSX.Element | null {
  if (chips.length === 0) return null;
  return (
    <div
      role="group"
      aria-label={ariaLabel}
      aria-live="polite"
      class={[
        'filter-chips flex flex-wrap items-center gap-1.5 px-3 py-1.5',
        'border-b border-slate-200 dark:border-slate-700 bg-slate-50/60 dark:bg-slate-900/40',
        className ?? '',
      ].join(' ')}
    >
      <span class="text-[10px] uppercase tracking-wide text-slate-500 dark:text-slate-400 mr-1">
        Filters
      </span>
      {chips.map((chip) => (
        <Pill
          key={chip.id}
          variant="info"
          size="sm"
          className="pl-2 pr-1 gap-1"
        >
          <span>{chip.label}</span>
          {chip.removable !== false ? (
            <button
              type="button"
              onClick={() => onRemove(chip)}
              aria-label={`Remove filter ${chip.label}`}
              class="inline-flex h-4 w-4 items-center justify-center rounded-sm text-current hover:bg-slate-300/60 dark:hover:bg-slate-500/60 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500"
            >
              <svg viewBox="0 0 20 20" fill="currentColor" class="w-3 h-3" aria-hidden="true">
                <path d="M6.28 5.22a.75.75 0 00-1.06 1.06L8.94 10l-3.72 3.72a.75.75 0 101.06 1.06L10 11.06l3.72 3.72a.75.75 0 101.06-1.06L11.06 10l3.72-3.72a.75.75 0 00-1.06-1.06L10 8.94 6.28 5.22z" />
              </svg>
            </button>
          ) : null}
        </Pill>
      ))}
      {chips.length > 1 && onClear ? (
        <Button variant="ghost" size="sm" onClick={onClear}>
          Clear
        </Button>
      ) : null}
    </div>
  );
}

export default FilterChips;
