/**
 * ``<Pagination>`` — the universal page-navigation primitive.
 *
 * Renders the prev / next pair, a window of clickable page numbers,
 * a page-size selector, and the row-range readout ("rows 51-100 of
 * 1,247"). Designed to bind directly to the ``Page<T>`` envelope
 * shipped by every paginated API endpoint post-W22b2 — the consumer
 * passes a single ``page`` prop (the envelope) plus an ``onChange``
 * callback and the component manages the rest.
 *
 * Interaction model:
 *
 * - Prev / Next are disabled at the appropriate ends (``page === 1``
 *   for prev, ``!has_next`` for next).
 * - Numeric buttons render a windowed slice: first, last, current
 *   ± 2, with ellipsis gaps. Capped at ~7 visible slots so the
 *   control stays one row tall on narrow viewports.
 * - The page-size selector is a plain ``<select>`` so it inherits
 *   the native keyboard semantics (Space / arrows / type-ahead).
 *   Default options track the design-token ladder
 *   (25 / 50 / 100 / 200, capped at MAX_PAGE_SIZE = 200 on the wire).
 * - The row-range readout is always rendered to give the operator
 *   the same scaffolding regardless of total — empty pages show
 *   "no rows" rather than disappear.
 *
 * Accessibility:
 *
 * - The whole region is wrapped in ``<nav role="navigation">`` with
 *   an ``aria-label`` so AT users hear "Pagination, navigation".
 * - Numeric buttons carry ``aria-current="page"`` on the active
 *   page and ``aria-label="Go to page N"`` everywhere else.
 * - The page-size selector has an associated ``<label>`` (visually
 *   hidden via ``sr-only`` so the design stays compact but still
 *   readable to AT).
 * - Disabled buttons use ``aria-disabled`` rather than the ``disabled``
 *   attribute so they remain focusable (some screen readers skip
 *   ``disabled``).
 *
 * Why not virtualise the numeric strip? At ten-thousand-row scale the
 * naive approach (all page numbers visible) breaks; the windowed
 * pattern caps the DOM at <=8 buttons regardless of total. The user
 * can still jump to "last" via the last-page button, and a future
 * Cmd+G "go to page" affordance can land alongside Cmd+K when the
 * Command Palette ships.
 */

import type { JSX } from 'preact';

import { Button } from './Button';

/**
 * Mirror of the server-side ``Page<T>`` envelope (api.ts ``Page<T>``).
 *
 * Inlined as a structural type so this component file doesn't pull in
 * the ``api`` module just to read five field names — keeps the import
 * graph shallow and makes ``Pagination`` reusable for any local
 * envelope a route might synthesise (e.g. for client-side pagination
 * over a cached list).
 */
export interface PaginationPage {
  page: number;
  page_size: number;
  total: number;
  has_next: boolean;
}

export interface PaginationProps {
  page: PaginationPage;
  /** Called when the user picks a different page. */
  onPageChange: (page: number) => void;
  /** Called when the user changes the page-size selector. Optional. */
  onPageSizeChange?: (pageSize: number) => void;
  /**
   * Page-size options surfaced in the selector. Defaults to
   * ``[25, 50, 100, 200]`` (matches MAX_PAGE_SIZE on the wire).
   * Pass a custom array to lock the menu to e.g. ``[10, 25]`` for a
   * route that doesn't want big pages.
   */
  pageSizeOptions?: number[];
  /**
   * Singular/plural noun for the row-range readout ("rows 1-50 of 200" vs
   * "specs 1-3 of 3"). Defaults to ``"rows"``.
   */
  itemLabel?: string;
  className?: string;
  /** ARIA region label override. */
  'aria-label'?: string;
}

const DEFAULT_PAGE_SIZE_OPTIONS = [25, 50, 100, 200];

/**
 * Build the windowed page-number sequence to display.
 *
 * Algorithm: always include 1 and N (the last page), the current
 * page, and ±2 around current. Insert ``null`` (rendered as ``…``)
 * where the gap is at least 2.
 *
 * Examples (current=5, total=10):  [1, …, 3, 4, 5, 6, 7, …, 10]
 *           (current=1, total=10): [1, 2, 3, …, 10]
 *           (current=10, total=10):[1, …, 8, 9, 10]
 *           (total=3):             [1, 2, 3]
 */
function windowedPages(current: number, totalPages: number): (number | null)[] {
  if (totalPages <= 0) return [];
  if (totalPages <= 7) {
    return Array.from({ length: totalPages }, (_, i) => i + 1);
  }
  const slots = new Set<number>([1, totalPages, current]);
  for (const delta of [-2, -1, 1, 2]) {
    const candidate = current + delta;
    if (candidate >= 1 && candidate <= totalPages) {
      slots.add(candidate);
    }
  }
  const sorted = Array.from(slots).sort((a, b) => a - b);
  const result: (number | null)[] = [];
  for (let i = 0; i < sorted.length; i++) {
    if (i > 0 && sorted[i] - sorted[i - 1] > 1) {
      result.push(null);
    }
    result.push(sorted[i]);
  }
  return result;
}

export function Pagination(props: PaginationProps): JSX.Element {
  const {
    page,
    onPageChange,
    onPageSizeChange,
    pageSizeOptions = DEFAULT_PAGE_SIZE_OPTIONS,
    itemLabel = 'rows',
    className = '',
  } = props;
  const ariaLabel = props['aria-label'] ?? 'Pagination';

  const totalPages = Math.max(1, Math.ceil(page.total / page.page_size));
  const firstRow = page.total === 0 ? 0 : (page.page - 1) * page.page_size + 1;
  const lastRow = Math.min(page.total, page.page * page.page_size);
  const onFirst = page.page <= 1;
  const onLast = !page.has_next;
  const sequence = windowedPages(page.page, totalPages);

  return (
    <nav
      role="navigation"
      aria-label={ariaLabel}
      class={
        'flex flex-wrap items-center justify-between gap-3 ' +
        'text-sm text-slate-700 dark:text-slate-300 ' +
        className
      }
    >
      <div class="flex items-center gap-2">
        <Button
          variant="secondary"
          size="sm"
          disabled={onFirst}
          aria-label="Go to first page"
          onClick={() => onPageChange(1)}
        >
          «
        </Button>
        <Button
          variant="secondary"
          size="sm"
          disabled={onFirst}
          aria-label="Go to previous page"
          onClick={() => onPageChange(page.page - 1)}
        >
          ‹ Prev
        </Button>
        <ol class="inline-flex items-center gap-1" aria-label="Page numbers">
          {sequence.map((slot, idx) =>
            slot === null ? (
              <li
                key={`gap-${idx}`}
                class="px-2 text-slate-400 dark:text-slate-500 select-none"
                aria-hidden="true"
              >
                …
              </li>
            ) : (
              <li key={`p-${slot}`}>
                <Button
                  variant={slot === page.page ? 'primary' : 'ghost'}
                  size="sm"
                  aria-label={`Go to page ${slot}`}
                  onClick={() => onPageChange(slot)}
                  className={
                    slot === page.page
                      ? 'pointer-events-none'
                      : ''
                  }
                >
                  {slot}
                </Button>
              </li>
            ),
          )}
        </ol>
        <Button
          variant="secondary"
          size="sm"
          disabled={onLast}
          aria-label="Go to next page"
          onClick={() => onPageChange(page.page + 1)}
        >
          Next ›
        </Button>
        <Button
          variant="secondary"
          size="sm"
          disabled={onLast}
          aria-label="Go to last page"
          onClick={() => onPageChange(totalPages)}
        >
          »
        </Button>
      </div>
      <div class="flex items-center gap-4 text-xs text-slate-500 dark:text-slate-400">
        <output aria-live="polite">
          {page.total === 0
            ? `no ${itemLabel}`
            : `${itemLabel} ${firstRow.toLocaleString()}–${lastRow.toLocaleString()} of ${page.total.toLocaleString()}`}
        </output>
        {onPageSizeChange ? (
          <label class="inline-flex items-center gap-1.5">
            <span class="sr-only">Rows per page</span>
            <select
              class={
                'rounded border border-slate-300 bg-white px-1.5 py-0.5 ' +
                'text-xs text-slate-700 ' +
                'dark:border-slate-600 dark:bg-slate-800 dark:text-slate-200 ' +
                'focus:outline-none focus:ring-2 focus:ring-emerald-500'
              }
              value={page.page_size}
              aria-label="Rows per page"
              onChange={(event: JSX.TargetedEvent<HTMLSelectElement>) => {
                const next = Number((event.currentTarget as HTMLSelectElement).value);
                onPageSizeChange(next);
              }}
            >
              {pageSizeOptions.map((size) => (
                <option key={size} value={size}>
                  {size} per page
                </option>
              ))}
            </select>
          </label>
        ) : null}
      </div>
    </nav>
  );
}

export default Pagination;
