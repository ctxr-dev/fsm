/**
 * ``<RunsSummaryStats>`` — the four-tile glance card above the /runs table.
 *
 * Renders Total / Active / Completed / Faulted counts pulled in
 * parallel from the paginated /runs endpoint. Each tile is a real
 * filter link: clicking a tile (other than Total) navigates to the
 * filtered runs list with that status pre-selected, so the operator's
 * "show me the faulted runs" instinct gets a one-click answer instead
 * of having to fiddle with the status dropdown.
 *
 * Why a separate component? Two reasons: (a) the runs route is
 * already ~600 LOC and keeping the parallel-fetch + tile-rendering
 * logic out of it keeps the route file focused on table + filter
 * state, and (b) a future "summary stats" call site might emerge on
 * the /drift or /journal routes — packaging the four-tile glance as
 * a primitive lets either route reuse it with a different
 * count-resolver.
 *
 * Data acquisition: four parallel ``api.listRuns({status, page_size: 1})``
 * calls. We only need ``Page.total`` for each — the cheapest way to
 * get a population count from a paginated endpoint without making the
 * server compute four full pages of rows we'll discard. ``page_size:
 * 1`` keeps the wire payload to one row + one count per call.
 *
 * Refresh contract: the parent passes a ``reloadKey`` (typically the
 * route's pagination/filter version stamp) so the stats re-fetch
 * whenever the underlying data could have shifted — a successful
 * abort/resume, a fresh SSE event for a status that's tracked here,
 * etc. The component owns its own loading state so a stat refresh
 * doesn't blank the table.
 */

import type { JSX } from 'preact';
import { useEffect, useState } from 'preact/hooks';

import { api, ApiError } from '../lib/api';

import { Card } from './Card';
import { Pill, type PillVariant } from './Pill';
import { Spinner } from './Spinner';

interface TileSpec {
  key: string;
  /** Human label rendered in the tile header. */
  label: string;
  /** Pill variant used for the status badge inside the tile. */
  variant: PillVariant;
  /** ``?status=`` value to apply when clicked. ``null`` for the
   *  "Total" tile which doesn't pre-filter. */
  statusFilter: string | null;
}

const TILES: readonly TileSpec[] = [
  { key: 'total', label: 'Total', variant: 'neutral', statusFilter: null },
  { key: 'in_progress', label: 'Active', variant: 'info', statusFilter: 'in_progress' },
  { key: 'completed', label: 'Completed', variant: 'success', statusFilter: 'completed' },
  { key: 'faulted', label: 'Faulted', variant: 'danger', statusFilter: 'faulted' },
];

export interface RunsSummaryStatsProps {
  /**
   * Composite reload key. Accepts ``string | number`` so callers can
   * thread multiple inputs (page, status, refresh-stamp) into a
   * single dependency without inventing a parent-side reducer; e.g.
   * ``reloadKey={\`${page}:${status}:${tick}\`}``.
   *
   * Pre-fix this was a bare ``number`` so a caller-side composite
   * value had to be JSON-encoded or hashed; the wider type matches
   * what useEffect actually allows as a dep.
   */
  reloadKey?: string | number;
  /**
   * Called when the user clicks a tile. Receives the tile's
   * ``statusFilter`` value (``null`` for Total). The parent route
   * applies it to its filter state + URL query.
   */
  onFilterPick?: (statusFilter: string | null) => void;
  className?: string;
}

export function RunsSummaryStats({
  reloadKey = 0,
  onFilterPick,
  className = '',
}: RunsSummaryStatsProps): JSX.Element {
  const [counts, setCounts] = useState<Record<string, number> | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    (async () => {
      // ``incomplete`` / ``resumable`` are the only special-keyword
      // statuses the /runs endpoint accepts; ``in_progress`` is a
      // literal status value (matches the StateStatus enum in the
      // engine). The "Total" tile sends no ``status`` so the server
      // returns the full population.
      //
      // ``Promise.allSettled`` NEVER rejects, so the outer try/catch
      // around it was dead code in the pre-fix draft — a 5xx on every
      // tile would leave the component in a "loading-forever" state
      // with no error surface. We now surface a degraded mode
      // explicitly: when ALL four tiles fail we set ``error`` so the
      // operator sees the failure; when SOME succeed we render the
      // partial counts with ``—`` for the failed ones and stash the
      // first failure reason so a future "what went wrong" affordance
      // (Cmd+K diagnostic, tooltip) can read it.
      const settled = await Promise.allSettled(
        TILES.map((t) =>
          api.listRuns({
            page_size: 1,
            page: 1,
            ...(t.statusFilter ? { status: t.statusFilter } : {}),
          }),
        ),
      );
      const next: Record<string, number> = {};
      let allFailed = true;
      let firstFailure: unknown = null;
      for (let i = 0; i < TILES.length; i++) {
        const result = settled[i];
        if (result.status === 'fulfilled') {
          next[TILES[i].key] = result.value.total;
          allFailed = false;
        } else {
          next[TILES[i].key] = -1;
          if (firstFailure === null) firstFailure = result.reason;
        }
      }
      if (cancelled) return;
      if (allFailed && firstFailure !== null) {
        setError(
          firstFailure instanceof ApiError
            ? firstFailure.message
            : String(firstFailure),
        );
        setCounts(null);
      } else {
        setCounts(next);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [reloadKey]);

  return (
    <Card className={className}>
      {error ? (
        <div class="p-3 text-sm text-red-700 dark:text-red-300">
          Couldn't load summary stats: {error}
        </div>
      ) : counts === null ? (
        <div class="flex items-center justify-center py-6">
          <Spinner label="Computing run summary" />
        </div>
      ) : (
        <div class="grid grid-cols-2 gap-3 p-3 md:grid-cols-4">
          {TILES.map((t) => {
            const count = counts[t.key] ?? -1;
            const isClickable = onFilterPick !== undefined;
            const content = (
              <>
                <div class="flex items-center justify-between gap-2">
                  <span class="text-[10px] font-medium uppercase tracking-wider text-slate-500 dark:text-slate-400">
                    {t.label}
                  </span>
                  <Pill variant={t.variant} size="sm">
                    {t.statusFilter ?? 'all'}
                  </Pill>
                </div>
                <div class="mt-1.5 text-2xl font-semibold leading-none text-slate-900 dark:text-slate-100 tabular-nums">
                  {count < 0 ? '—' : count.toLocaleString()}
                </div>
              </>
            );
            return isClickable ? (
              <button
                key={t.key}
                type="button"
                onClick={() => onFilterPick(t.statusFilter)}
                class={
                  'rounded-md border border-slate-200 bg-white p-3 text-left ' +
                  'transition-colors hover:border-emerald-400 hover:bg-emerald-50/40 ' +
                  'focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500 ' +
                  'dark:border-slate-700 dark:bg-slate-800 ' +
                  'dark:hover:border-emerald-500 dark:hover:bg-emerald-900/20'
                }
                aria-label={`Filter to ${t.statusFilter ?? 'all'} runs (${count < 0 ? 'unknown' : count} total)`}
              >
                {content}
              </button>
            ) : (
              <div
                key={t.key}
                class={
                  'rounded-md border border-slate-200 bg-white p-3 ' +
                  'dark:border-slate-700 dark:bg-slate-800'
                }
              >
                {content}
              </div>
            );
          })}
        </div>
      )}
    </Card>
  );
}

export default RunsSummaryStats;
