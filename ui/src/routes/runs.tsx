/**
 * /runs — run list page.
 *
 * Hosts the operator's primary "what's happening right now" surface:
 *
 * * Sticky filter bar (status pills + search input + refresh button).
 * * Paginated <Table> of run summaries keyed by ``RunSummary.id``.
 * * Click / Enter on a row routes to ``/runs/:id``.
 * * Skeleton during the first fetch; <EmptyState> with a CLI hint when
 *   the API returns zero rows for the active filter.
 * * Pagination is offset-based and round-trips through the URL
 *   (``/runs?offset=20``) so the page is shareable / refreshable.
 * * Keyboard: ``/`` focuses the search input (without inserting the
 *   slash); ``j`` / ``k`` walks the row cursor.
 * * Subscribes to the global ``eventLog`` signal — whenever a
 *   ``run_started`` / ``run_completed`` / ``run_aborted`` event
 *   arrives, the list refetches so the table stays live without a
 *   manual refresh.
 *
 * State lives in two places by design: filter / search / offset are
 * URL-driven (so links from chat or terminal history land on the
 * exact view), while the row cursor is local component state because
 * it is ephemeral and per-tab.
 */

import type { JSX, VNode } from 'preact';
import { useCallback, useEffect, useMemo, useRef, useState } from 'preact/hooks';
import { useLocation } from 'preact-iso';

import {
  Button,
  Card,
  EmptyState,
  Pill,
  Spinner,
  Table,
  type PillVariant,
  type TableColumn,
} from '../components';
import { api, ApiError, type RunSummary } from '../lib/api';
import { eventLog } from '../lib/store';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/** Status filter chips exposed in the sticky filter bar. */
const STATUS_FILTERS: readonly { value: string; label: string }[] = [
  { value: 'all', label: 'All' },
  { value: 'in_progress', label: 'In progress' },
  { value: 'paused', label: 'Paused' },
  { value: 'faulted', label: 'Faulted' },
  { value: 'completed', label: 'Completed' },
  { value: 'aborted', label: 'Aborted' },
];

/** Default page size — kept modest so the first paint is cheap. */
const PAGE_SIZE = 20;

/** Event kinds that should trigger a list refresh. */
const REFRESH_EVENT_KINDS: ReadonlySet<string> = new Set([
  'run_started',
  'run_completed',
  'run_aborted',
]);

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Map an FSM run ``status`` to a :class:`Pill` colour variant.
 *
 * Centralised so the dashboard, the detail header, and this list all
 * agree on the colour language ("emerald = success, red = danger,
 * amber = attention, slate = idle/neutral").
 */
function statusVariant(status: string): PillVariant {
  switch (status) {
    case 'completed':
      return 'success';
    case 'in_progress':
      return 'info';
    case 'paused':
      return 'warning';
    case 'faulted':
      return 'danger';
    case 'aborted':
      return 'danger';
    default:
      return 'neutral';
  }
}

/**
 * Short, copy-friendly prefix of a run id (first 7 chars, git-style).
 *
 * The full id remains accessible via the row's ``title`` attribute so
 * power users can hover to see the canonical value.
 */
function shortId(id: string): string {
  return id.length > 7 ? id.slice(0, 7) : id;
}

/**
 * Render an ISO-8601 timestamp as a locale-aware short string.
 *
 * Falls back to the raw value if ``Date`` cannot parse it — better to
 * surface "garbage in, garbage out" than to silently hide the cell.
 */
function formatTimestamp(value: string | null): string {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

/**
 * Compose the ``spec`` column cell — ``slug:version`` if both are
 * derivable, otherwise the raw ``fsm_spec_id``.
 *
 * The API currently exposes only ``fsm_spec_id`` on :class:`RunSummary`
 * (no embedded slug). We display the id directly; a future server-side
 * enrichment will swap this for a real slug:version pair without
 * touching the callsite.
 */
function specCell(run: RunSummary): string {
  return run.fsm_spec_id;
}

/**
 * Parse ``?offset=NN`` from the current URL search string.
 *
 * Tolerates missing / non-numeric / negative values by snapping to 0
 * so the page never enters an invalid pagination state.
 */
function parseOffset(search: string): number {
  const params = new URLSearchParams(
    search.startsWith('?') ? search.slice(1) : search,
  );
  const raw = params.get('offset');
  if (!raw) return 0;
  const n = Number.parseInt(raw, 10);
  if (!Number.isFinite(n) || n < 0) return 0;
  return n;
}

/**
 * Read the current ``window.location.search`` safely under SSR / tests
 * where ``window`` may be absent.
 */
function currentSearch(): string {
  if (typeof window === 'undefined') return '';
  return window.location.search ?? '';
}

/**
 * Build a ``/runs`` URL with the supplied offset, preserving any
 * other search params (forward-compat for future filters).
 */
function buildOffsetUrl(offset: number): string {
  const params = new URLSearchParams(currentSearch());
  if (offset > 0) {
    params.set('offset', String(offset));
  } else {
    params.delete('offset');
  }
  const qs = params.toString();
  return qs ? `/runs?${qs}` : '/runs';
}

// ---------------------------------------------------------------------------
// Filter bar
// ---------------------------------------------------------------------------

interface FilterBarProps {
  status: string;
  search: string;
  loading: boolean;
  onStatusChange: (next: string) => void;
  onSearchChange: (next: string) => void;
  onRefresh: () => void;
  searchInputRef: { current: HTMLInputElement | null };
}

/**
 * Sticky filter bar — status chips + search box + refresh button.
 *
 * Kept as a sub-component so the parent's render tree stays focused on
 * data orchestration; the bar itself is purely a controlled-input
 * surface and is trivially testable in isolation.
 */
function FilterBar({
  status,
  search,
  loading,
  onStatusChange,
  onSearchChange,
  onRefresh,
  searchInputRef,
}: FilterBarProps): JSX.Element {
  return (
    <div
      class={[
        'sticky top-0 z-20',
        'bg-slate-50/95 dark:bg-slate-900/95 backdrop-blur',
        'border-b border-slate-200 dark:border-slate-700',
        'px-4 py-3',
      ].join(' ')}
    >
      <div class="flex flex-wrap items-center gap-3">
        <div
          role="tablist"
          aria-label="Filter runs by status"
          class="flex flex-wrap items-center gap-1"
        >
          {STATUS_FILTERS.map((f) => {
            const active = f.value === status;
            return (
              <button
                key={f.value}
                type="button"
                role="tab"
                aria-selected={active}
                onClick={() => onStatusChange(f.value)}
                class={[
                  'inline-flex items-center rounded-full px-3 py-1',
                  'text-xs font-medium transition-colors',
                  'focus:outline-none focus-visible:ring-2 focus-visible:ring-offset-2',
                  'focus-visible:ring-emerald-500',
                  'dark:focus-visible:ring-offset-slate-900',
                  active
                    ? 'bg-emerald-600 text-white shadow-sm'
                    : 'bg-slate-100 text-slate-700 hover:bg-slate-200 ' +
                      'dark:bg-slate-700 dark:text-slate-200 dark:hover:bg-slate-600',
                ].join(' ')}
              >
                {f.label}
              </button>
            );
          })}
        </div>
        <div class="flex-1 min-w-[12rem]">
          <label class="sr-only" htmlFor="runs-search">
            Search runs
          </label>
          <input
            id="runs-search"
            ref={(el) => {
              searchInputRef.current = el;
            }}
            type="search"
            placeholder="Search by id or spec… (press /)"
            value={search}
            onInput={(event) =>
              onSearchChange((event.currentTarget as HTMLInputElement).value)
            }
            class={[
              'block w-full rounded-md',
              'border border-slate-300 dark:border-slate-600',
              'bg-white dark:bg-slate-800',
              'text-sm text-slate-900 dark:text-slate-100',
              'placeholder:text-slate-400 dark:placeholder:text-slate-500',
              'px-3 py-2',
              'focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500',
              'focus-visible:border-emerald-500',
            ].join(' ')}
          />
        </div>
        <Button
          variant="secondary"
          size="sm"
          onClick={onRefresh}
          loading={loading}
          aria-label="Refresh run list"
        >
          Refresh
        </Button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Pagination
// ---------------------------------------------------------------------------

interface PaginationProps {
  offset: number;
  pageSize: number;
  rowCount: number;
  hasNext: boolean;
  onPrev: () => void;
  onNext: () => void;
}

/**
 * Offset-based prev/next pager.
 *
 * ``hasNext`` is computed by the parent: if the API returned exactly
 * ``pageSize`` rows there *might* be more, so we enable the button;
 * if it returned fewer, we know we've hit the tail and disable it.
 */
function Pagination({
  offset,
  pageSize,
  rowCount,
  hasNext,
  onPrev,
  onNext,
}: PaginationProps): JSX.Element {
  const from = rowCount === 0 ? 0 : offset + 1;
  const to = offset + rowCount;
  return (
    <div class="flex items-center justify-between px-4 py-3 text-sm text-slate-600 dark:text-slate-400">
      <span aria-live="polite">
        {rowCount === 0
          ? 'No results'
          : `Showing ${from}–${to}`}
      </span>
      <div class="flex items-center gap-2">
        <Button
          variant="ghost"
          size="sm"
          onClick={onPrev}
          disabled={offset === 0}
          aria-label="Previous page"
        >
          Prev
        </Button>
        <Button
          variant="ghost"
          size="sm"
          onClick={onNext}
          disabled={!hasNext}
          aria-label="Next page"
        >
          Next
        </Button>
        <span class="text-xs text-slate-500 dark:text-slate-500">
          page {Math.floor(offset / pageSize) + 1}
        </span>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Empty-state icon
// ---------------------------------------------------------------------------

/**
 * Inline SVG used by the runs empty state.
 *
 * Inlined (rather than an external asset) so the empty state survives
 * offline / restricted-CSP deployments and so the icon participates
 * in dark-mode colour via ``currentColor``.
 */
function EmptyIcon(): VNode {
  return (
    <svg
      width="48"
      height="48"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="1.5"
      stroke-linecap="round"
      stroke-linejoin="round"
      aria-hidden="true"
    >
      <rect x="3" y="4" width="18" height="16" rx="2" />
      <path d="M3 9h18" />
      <path d="M8 14h2" />
      <path d="M8 17h6" />
    </svg>
  );
}

// ---------------------------------------------------------------------------
// Route component
// ---------------------------------------------------------------------------

export function Runs(): JSX.Element {
  const location = useLocation();

  // URL-driven state — read on mount and on every navigation.
  const initialOffset = parseOffset(currentSearch());
  const [offset, setOffset] = useState<number>(initialOffset);
  const [status, setStatus] = useState<string>('all');
  const [search, setSearch] = useState<string>('');

  // Server / interaction state.
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [cursor, setCursor] = useState<number>(0);

  // Track which event we've already reacted to so the SSE refetch
  // fires once per new event, not on every signal subscriber tick.
  const lastSeenEventId = useRef<string | null>(null);
  const searchInputRef = useRef<HTMLInputElement | null>(null);

  // --- Data fetch -----------------------------------------------------------

  const fetchRuns = useCallback(
    async (opts: { offset: number; status: string }) => {
      setLoading(true);
      setError(null);
      try {
        const params =
          opts.status === 'all'
            ? { limit: PAGE_SIZE, offset: opts.offset }
            : { status: opts.status, limit: PAGE_SIZE, offset: opts.offset };
        const result = await api.listRuns(params);
        setRuns(result);
        setCursor((c) => (result.length === 0 ? 0 : Math.min(c, result.length - 1)));
      } catch (err) {
        const msg =
          err instanceof ApiError
            ? `Failed to load runs (HTTP ${err.status})`
            : err instanceof Error
              ? err.message
              : 'Failed to load runs';
        setError(msg);
        setRuns([]);
      } finally {
        setLoading(false);
      }
    },
    [],
  );

  // Initial + offset/status driven fetch.
  useEffect(() => {
    void fetchRuns({ offset, status });
  }, [fetchRuns, offset, status]);

  // --- Live refresh from SSE ------------------------------------------------

  useEffect(() => {
    // ``eventLog`` is a signal — accessing ``.value`` inside the effect
    // registers this effect as a subscriber so it re-runs on each change.
    const log = eventLog.value;
    if (log.length === 0) return;
    const newest = log[log.length - 1];
    if (!newest) return;
    if (lastSeenEventId.current === newest.id) return;
    lastSeenEventId.current = newest.id;
    if (REFRESH_EVENT_KINDS.has(newest.kind)) {
      void fetchRuns({ offset, status });
    }
  }, [fetchRuns, offset, status]);

  // --- Filtering ------------------------------------------------------------

  const visibleRuns = useMemo(() => {
    if (!search.trim()) return runs;
    const needle = search.trim().toLowerCase();
    return runs.filter(
      (r) =>
        r.id.toLowerCase().includes(needle) ||
        r.fsm_spec_id.toLowerCase().includes(needle),
    );
  }, [runs, search]);

  // --- Keyboard navigation --------------------------------------------------

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      const target = event.target as HTMLElement | null;
      const inField =
        target &&
        (target.tagName === 'INPUT' ||
          target.tagName === 'TEXTAREA' ||
          target.tagName === 'SELECT' ||
          target.isContentEditable);

      if (event.key === '/' && !inField) {
        event.preventDefault();
        searchInputRef.current?.focus();
        return;
      }

      if (inField) return;

      if (event.key === 'j') {
        event.preventDefault();
        setCursor((c) => Math.min(c + 1, Math.max(visibleRuns.length - 1, 0)));
      } else if (event.key === 'k') {
        event.preventDefault();
        setCursor((c) => Math.max(c - 1, 0));
      } else if (event.key === 'Enter') {
        const row = visibleRuns[cursor];
        if (row) {
          event.preventDefault();
          location.route(`/runs/${row.id}`);
        }
      }
    }
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [cursor, location, visibleRuns]);

  // --- Pagination handlers --------------------------------------------------

  const goToOffset = useCallback(
    (next: number) => {
      const clamped = Math.max(0, next);
      setOffset(clamped);
      location.route(buildOffsetUrl(clamped));
    },
    [location],
  );

  const onPrev = useCallback(() => {
    goToOffset(Math.max(0, offset - PAGE_SIZE));
  }, [goToOffset, offset]);

  const onNext = useCallback(() => {
    goToOffset(offset + PAGE_SIZE);
  }, [goToOffset, offset]);

  // --- Table columns --------------------------------------------------------

  const columns: TableColumn<RunSummary>[] = useMemo(
    () => [
      {
        key: 'id',
        label: 'Id',
        width: '8rem',
        render: (r) => (
          <code
            class="font-mono text-xs text-slate-700 dark:text-slate-300"
            title={r.id}
          >
            {shortId(r.id)}
          </code>
        ),
      },
      {
        key: 'spec',
        label: 'Spec',
        render: (r) => (
          <span class="text-sm text-slate-800 dark:text-slate-200">
            {specCell(r)}
          </span>
        ),
      },
      {
        key: 'status',
        label: 'Status',
        width: '8rem',
        render: (r) => (
          <Pill variant={statusVariant(r.status)} size="sm">
            {r.status}
          </Pill>
        ),
      },
      {
        key: 'started_at',
        label: 'Started',
        width: '12rem',
        render: (r) => (
          <span class="text-xs text-slate-600 dark:text-slate-400">
            {formatTimestamp(r.started_at)}
          </span>
        ),
      },
      {
        key: 'last_update_at',
        label: 'Last update',
        width: '12rem',
        render: (r) => (
          <span class="text-xs text-slate-600 dark:text-slate-400">
            {formatTimestamp(r.last_update_at)}
          </span>
        ),
      },
    ],
    [],
  );

  // --- Body content ---------------------------------------------------------

  const onRowClick = useCallback(
    (row: RunSummary) => {
      location.route(`/runs/${row.id}`);
    },
    [location],
  );

  const onRefresh = useCallback(() => {
    void fetchRuns({ offset, status });
  }, [fetchRuns, offset, status]);

  const hasNext = runs.length === PAGE_SIZE;

  let body: VNode;
  if (loading && runs.length === 0) {
    body = (
      <Card className="flex items-center justify-center py-16">
        <Spinner size="lg" label="Loading runs" />
      </Card>
    );
  } else if (error) {
    body = (
      <Card>
        <EmptyState
          title="Couldn't load runs"
          message={error}
          action={
            <Button variant="primary" size="md" onClick={onRefresh}>
              Try again
            </Button>
          }
        />
      </Card>
    );
  } else if (visibleRuns.length === 0) {
    body = (
      <Card className="p-0">
        <EmptyState
          icon={<EmptyIcon />}
          title="No runs yet"
          message={
            'Start a run via the CLI or MCP server. The CLI command is ' +
            '`ctxr-fsm run start <spec-id> --args ...`.'
          }
        />
      </Card>
    );
  } else {
    body = (
      <Card className="p-0">
        <Table<RunSummary>
          caption="FSM runs"
          columns={columns}
          rows={visibleRuns}
          onRowClick={onRowClick}
          rowKey={(r) => r.id}
        />
        <Pagination
          offset={offset}
          pageSize={PAGE_SIZE}
          rowCount={visibleRuns.length}
          hasNext={hasNext}
          onPrev={onPrev}
          onNext={onNext}
        />
      </Card>
    );
  }

  return (
    <div class="min-h-screen bg-slate-50 dark:bg-slate-900">
      <FilterBar
        status={status}
        search={search}
        loading={loading}
        onStatusChange={(next) => {
          setStatus(next);
          // Switching the filter resets pagination — otherwise the
          // user can land on an empty page-3 of a freshly-narrowed set.
          if (offset !== 0) goToOffset(0);
        }}
        onSearchChange={setSearch}
        onRefresh={onRefresh}
        searchInputRef={searchInputRef}
      />
      <main class="p-4">
        <div class="mb-3 flex items-baseline justify-between">
          <h1 class="text-2xl font-semibold text-slate-900 dark:text-slate-100">
            Runs
          </h1>
          <span class="text-xs text-slate-500 dark:text-slate-400">
            {loading ? 'Refreshing…' : `${visibleRuns.length} shown`}
          </span>
        </div>
        {body}
      </main>
    </div>
  );
}

export default Runs;
