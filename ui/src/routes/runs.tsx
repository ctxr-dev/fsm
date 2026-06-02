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
 * * Pagination is page-based (``Page<RunSummary>`` envelope) and
 *   currently lives in component state; URL serialisation
 *   (``?page=&page_size=&sort=``) is a planned follow-up.
 * * Keyboard: ``/`` focuses the search input (without inserting the
 *   slash); ``j`` / ``k`` walks the row cursor.
 * * Subscribes to the global ``eventLog`` signal — whenever a
 *   ``run_started`` / ``run_completed`` / ``run_aborted`` event
 *   arrives, the list refetches so the table stays live without a
 *   manual refresh.
 *
 * State lives in local component hooks: filter / search / page /
 * page-size / cursor. URL serialisation of filter+page state is a
 * planned follow-up so links from chat or terminal history can land
 * on the exact view; for now the cursor is intentionally ephemeral
 * and per-tab.
 */

import type { JSX, VNode } from 'preact';
import { useCallback, useEffect, useMemo, useRef, useState } from 'preact/hooks';
import { useLocation } from 'preact-iso';

import {
  Button,
  Card,
  EmptyState,
  MultiSelectCombobox,
  Pagination,
  Pill,
  RunsSummaryStats,
  Spinner,
  Table,
  type PillVariant,
  type TableColumn,
} from '../components';
import { useProjectPref } from '../lib/projectPrefs';
import {
  api,
  ApiError,
  walkAllPages,
  type ListRunsParams,
  type Page,
  type RunSummary,
  type SpecSummary,
} from '../lib/api';
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
const DEFAULT_PAGE_SIZE = 25;

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
 * Render a duration in human-friendly compact form: ``45s``, ``3m 12s``,
 * ``1h 04m``. ``end`` defaults to "now" for in-flight runs. Returns
 * ``—`` when the start timestamp is missing / unparseable. Uses
 * integer-only display past 60 seconds to keep the column tabular-
 * aligned without sub-second jitter on live SSE updates.
 */
function formatDuration(start: string | null, end: string | null): string {
  if (!start) return '—';
  const t0 = new Date(start).getTime();
  if (Number.isNaN(t0)) return '—';
  const t1 = end ? new Date(end).getTime() : Date.now();
  if (Number.isNaN(t1) || t1 < t0) return '—';
  const sec = Math.floor((t1 - t0) / 1000);
  if (sec < 60) return `${sec}s`;
  const min = Math.floor(sec / 60);
  const s = sec % 60;
  if (min < 60) return `${min}m ${s.toString().padStart(2, '0')}s`;
  const h = Math.floor(min / 60);
  const m = min % 60;
  return `${h}h ${m.toString().padStart(2, '0')}m`;
}

/**
 * Map a run's `fsm_spec_id` to a human-readable label using the
 * resolver populated by a one-shot `api.listSpecs()` call. Falls
 * back to the raw UUID when the spec isn't in the cache (a race or
 * a stale resolver after the run was created from a freshly
 * registered spec).
 */
function specCell(run: RunSummary, resolver: Map<string, { slug: string; version: number }>): string {
  const entry = resolver.get(run.fsm_spec_id);
  if (entry) return `${entry.slug} v${entry.version}`;
  return run.fsm_spec_id.slice(0, 12) + '…';
}

// Spec resolver bootstrap delegates to the shared ``walkAllPages``
// helper in ``lib/api.ts``. Pre-W22b2-iter we had a bespoke inline
// loop here that capped at 10 pages of 200 (2000 specs); the shared
// helper offers a 50-page default with the same per-page size. The
// resolver needs every spec the project has ever registered, not
// just the first page, so the Spec column on each run row can render
// ``slug v<n>`` rather than a raw UUID.

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
  // W23d: spec multiselect filter — the dropdown options + the
  // selection state are managed by the parent so the persistence layer
  // (useProjectPref) lives in one place.
  specOptions: readonly { id: string; label: string; sub: string }[];
  selectedSpecIds: Set<string>;
  onSpecsChange: (next: Set<string>) => void;
}

/**
 * Sticky filter bar — status chips + spec multiselect + search box + refresh.
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
  specOptions,
  selectedSpecIds,
  onSpecsChange,
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
                aria-selected={active ? 'true' : 'false'}
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
        {/* W23d: spec multiselect filter. Only rendered when there's
            at least one spec to pick from; otherwise the dropdown
            would surface "no options" with no recovery. */}
        {specOptions.length > 0 ? (
          <MultiSelectCombobox
            options={specOptions}
            selected={selectedSpecIds}
            onChange={onSpecsChange}
            getId={(o) => o.id}
            getLabel={(o) => o.label}
            getSubLabel={(o) => o.sub}
            placeholder="All specs"
            searchPlaceholder="Filter by slug…"
            ariaLabel="Filter runs by spec"
          />
        ) : null}
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

  // Pagination + filter state. Page-based (1-indexed) post-W22b3 to
  // match the ``Page<T>`` envelope returned by ``api.listRuns``.
  // URL state for these (``?page=&page_size=&sort=``) is a follow-up;
  // for now they live in component state.
  const [page, setPage] = useState<number>(1);
  const [pageSize, setPageSize] = useState<number>(DEFAULT_PAGE_SIZE);
  const [status, setStatus] = useState<string>('all');
  const [search, setSearch] = useState<string>('');

  // Server / interaction state.
  const [runsPage, setRunsPage] = useState<Page<RunSummary> | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [cursor, setCursor] = useState<number>(0);
  const [specResolver, setSpecResolver] = useState<Map<string, { slug: string; version: number }>>(
    new Map(),
  );

  // Fetch the spec list once on mount + build a resolver map so the
  // Spec column on each row can render slug + version instead of a
  // raw UUID. Best-effort: a failure leaves the resolver empty so the
  // fallback ("12-char prefix...") still works. Walks pages because
  // the project may have more specs than fit in one page.
  useEffect(() => {
    let cancelled = false;
    walkAllPages((p) => api.listSpecs(p), {}, 10)
      .then((specs: SpecSummary[]) => {
        if (cancelled) return;
        const m = new Map<string, { slug: string; version: number }>();
        for (const s of specs) m.set(s.id, { slug: s.slug, version: s.version });
        setSpecResolver(m);
      })
      .catch(() => {
        // Silent — the resolver stays empty and rows render with the
        // UUID-prefix fallback.
      });
    return () => { cancelled = true; };
  }, []);

  // Track which event we've already reacted to so the SSE refetch
  // fires once per new event, not on every signal subscriber tick.
  const lastSeenEventId = useRef<string | null>(null);
  const searchInputRef = useRef<HTMLInputElement | null>(null);

  // --- Data fetch -----------------------------------------------------------

  const fetchRuns = useCallback(
    async (opts: { page: number; pageSize: number; status: string }) => {
      setLoading(true);
      setError(null);
      try {
        const params: ListRunsParams = {
          page: opts.page,
          page_size: opts.pageSize,
        };
        if (opts.status !== 'all') params.status = opts.status;
        const result = await api.listRuns(params);
        setRunsPage(result);
        setCursor((c) =>
          result.items.length === 0 ? 0 : Math.min(c, result.items.length - 1),
        );
      } catch (err) {
        const msg =
          err instanceof ApiError
            ? `Failed to load runs (HTTP ${err.status})`
            : err instanceof Error
              ? err.message
              : 'Failed to load runs';
        setError(msg);
        setRunsPage(null);
      } finally {
        setLoading(false);
      }
    },
    [],
  );

  // Initial + page/pageSize/status driven fetch.
  useEffect(() => {
    void fetchRuns({ page, pageSize, status });
  }, [fetchRuns, page, pageSize, status]);

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
      void fetchRuns({ page, pageSize, status });
    }
  }, [fetchRuns, page, pageSize, status]);

  // --- Filtering ------------------------------------------------------------

  // W23d: per-project spec multiselect filter. Persisted to
  // localStorage under fsm-ui:proj:<projectName>:runs.specFilter so
  // the selection survives reloads and stays isolated to the
  // current project. Stored as array (JSON-serialisable), hydrated
  // into a Set for O(1) membership tests during filtering.
  const [selectedSpecIdsArray, setSelectedSpecIdsArray] =
    useProjectPref<string[]>('runs.specFilter', []);
  const selectedSpecIds = useMemo(
    () => new Set(selectedSpecIdsArray),
    [selectedSpecIdsArray],
  );

  // Options for the multiselect: every spec in the resolver, sorted
  // by slug asc + numeric version desc. We derive afresh whenever the
  // resolver populates so the dropdown reflects every spec the project
  // has registered. Sorting on the rendered label string would
  // lexicographically order "v10" before "v2"; sort on (slug, version)
  // primitives so "code-reviewer v10" sits above "code-reviewer v2".
  const specOptions = useMemo(() => {
    const out: Array<{
      id: string;
      label: string;
      sub: string;
      slug: string;
      version: number;
    }> = [];
    for (const [id, { slug, version }] of specResolver.entries()) {
      out.push({
        id,
        label: `${slug} v${version}`,
        sub: id.slice(0, 12) + '…',
        slug,
        version,
      });
    }
    out.sort((a, b) => {
      const bySlug = a.slug.localeCompare(b.slug);
      if (bySlug !== 0) return bySlug;
      return b.version - a.version;
    });
    return out;
  }, [specResolver]);

  // Auto-prune stale selections — if a spec was deleted upstream and
  // its id still sits in the persisted filter, drop it so the
  // operator doesn't see an "empty filter" with no recovery hint.
  useEffect(() => {
    if (selectedSpecIdsArray.length === 0 || specResolver.size === 0) return;
    const valid = new Set(specResolver.keys());
    const pruned = selectedSpecIdsArray.filter((id) => valid.has(id));
    if (pruned.length !== selectedSpecIdsArray.length) {
      setSelectedSpecIdsArray(pruned);
    }
  }, [selectedSpecIdsArray, specResolver, setSelectedSpecIdsArray]);

  const runs = runsPage?.items ?? [];
  const visibleRuns = useMemo(() => {
    let filtered = runs;
    if (selectedSpecIds.size > 0) {
      filtered = filtered.filter((r) => selectedSpecIds.has(r.fsm_spec_id));
    }
    if (search.trim()) {
      const needle = search.trim().toLowerCase();
      filtered = filtered.filter(
        (r) =>
          r.id.toLowerCase().includes(needle) ||
          r.fsm_spec_id.toLowerCase().includes(needle),
      );
    }
    return filtered;
  }, [runs, search, selectedSpecIds]);

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

  const onPageChange = useCallback((next: number) => {
    setPage(Math.max(1, next));
  }, []);

  const onPageSizeChange = useCallback((next: number) => {
    setPageSize(next);
    // Snap back to page 1 so the new window starts at the top of the
    // population, not partway through (e.g. doubling page-size from
    // page 4 of 25 would otherwise leave the user mid-list).
    setPage(1);
  }, []);

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
          <a
            href={`/specs/${encodeURIComponent(r.fsm_spec_id)}`}
            class="text-sm text-slate-800 dark:text-slate-200 hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500 rounded-sm"
            title="Open spec"
            onClick={(e) => e.stopPropagation()}
          >
            {specCell(r, specResolver)}
          </a>
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
      {
        // W22b5: duration column. In-flight runs (``ended_at == null``)
        // compute the elapsed time against ``Date.now()`` at render
        // time, so the cell ticks forward on every refresh. Pre-W22b5
        // an operator wanting "how long has this been running" had to
        // mental-math the gap between Started and Last update — now
        // it's tabular.
        key: 'duration',
        label: 'Duration',
        width: '7rem',
        align: 'right' as const,
        render: (r) => (
          <span class="text-xs tabular-nums text-slate-700 dark:text-slate-300">
            {formatDuration(r.started_at, r.ended_at)}
          </span>
        ),
      },
    ],
    // W19: re-create columns when the spec resolver populates so the
    // Spec column re-renders from `019e80be-ebe…` → `code-reviewer v1`.
    // Without `specResolver` here the empty initial Map is captured.
    [specResolver],
  );

  // --- Body content ---------------------------------------------------------

  const onRowClick = useCallback(
    (row: RunSummary) => {
      location.route(`/runs/${row.id}`);
    },
    [location],
  );

  const onRefresh = useCallback(() => {
    void fetchRuns({ page, pageSize, status });
  }, [fetchRuns, page, pageSize, status]);

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
        {runsPage ? (
          <div class="border-t border-slate-200 px-4 py-3 dark:border-slate-700">
            <Pagination
              page={runsPage}
              onPageChange={onPageChange}
              onPageSizeChange={onPageSizeChange}
              itemLabel="runs"
            />
          </div>
        ) : null}
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
          if (page !== 1) setPage(1);
        }}
        onSearchChange={setSearch}
        onRefresh={onRefresh}
        searchInputRef={searchInputRef}
        specOptions={specOptions}
        selectedSpecIds={selectedSpecIds}
        onSpecsChange={(next) => setSelectedSpecIdsArray([...next])}
      />
      <main class="p-4 space-y-4">
        <div class="flex items-baseline justify-between">
          <h1 class="text-2xl font-semibold text-slate-900 dark:text-slate-100">
            Runs
          </h1>
          <span class="text-xs text-slate-500 dark:text-slate-400">
            {loading ? 'Refreshing…' : `${visibleRuns.length} shown`}
          </span>
        </div>
        {/* W22b5: four-tile glance card. Reloads in lockstep with
            page / status changes so the count tiles match whatever
            the table is showing right now. Clicking a tile re-applies
            its status filter to the table (and resets the cursor to
            page 1). */}
        <RunsSummaryStats
          reloadKey={page}
          onFilterPick={(nextStatus) => {
            setStatus(nextStatus ?? 'all');
            if (page !== 1) setPage(1);
          }}
        />
        {body}
      </main>
    </div>
  );
}

export default Runs;
