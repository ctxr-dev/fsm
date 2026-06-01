/**
 * /specs — spec catalog (W19 spec-first IA landing page).
 *
 * Lists every registered spec, GROUPED BY SLUG, ordered by most-
 * recently-active first (last_update_at of any run of that slug). Each
 * row surfaces:
 *
 *   - slug + latest version pill
 *   - total runs across all versions
 *   - newest run's status pill + when it last updated
 *   - number of registered versions
 *
 * Clicking a row navigates to /specs/:specId (the W19 spec-scoped
 * dashboard with Graph / Runs / Schemas / Definition / Versions
 * tabs). This page replaces the previous master/detail split — the
 * detail view is now its own URL.
 *
 * If no specs are registered, the empty state hints at the CLI
 * command users typically run to register one.
 */

import type { JSX } from 'preact';
import { useEffect, useMemo, useState } from 'preact/hooks';

import {
  Card,
  EmptyState,
  Pill,
  Spinner,
  Table,
  type PillVariant,
  type TableColumn,
} from '../components';
import {
  api,
  ApiError,
  type Page,
  type PageParams,
  type RunSummary,
  type SpecSummary,
} from '../lib/api';

/**
 * Walk a paginated endpoint until ``has_next`` is false (or the safety
 * stop trips at ``maxPages``). Used by routes that need to enumerate
 * the full population for derivative aggregations — e.g. the spec
 * catalog's per-slug run count or sibling tree. Returns the flattened
 * items list. Pages are fetched serially to keep the server's
 * paginator's cursor / sort stable; ``page_size`` is fixed at the
 * wire MAX_PAGE_SIZE of 200 to minimise round trips.
 */
async function walkAllPages<T, P extends PageParams>(
  fetcher: (params: P) => Promise<Page<T>>,
  baseParams: Omit<P, 'page' | 'page_size'>,
  maxPages = 50,
): Promise<T[]> {
  const out: T[] = [];
  for (let page = 1; page <= maxPages; page += 1) {
    const env = await fetcher({ ...(baseParams as P), page, page_size: 200 } as P);
    out.push(...env.items);
    if (!env.has_next) return out;
  }
  return out;
}

const shortHash = (h: string): string => (h.length > 12 ? h.slice(0, 12) : h);

function formatTimestamp(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

function statusVariant(status: string): PillVariant {
  switch (status) {
    case 'completed': return 'success';
    case 'in_progress': return 'info';
    case 'paused': return 'warning';
    case 'faulted':
    case 'aborted':
    case 'drift_paused': return 'danger';
    default: return 'neutral';
  }
}

interface SpecGroupRow {
  /** The latest registered SpecSummary for this slug (used for the navigation target). */
  latest: SpecSummary;
  /** Number of registered versions of this slug. */
  versionCount: number;
  /** Number of runs across all versions of this slug. */
  runCount: number;
  /** Newest run for this slug (any version), or null if none. */
  newestRun: RunSummary | null;
}

/**
 * Build the per-slug rows: group specs by slug, take the latest
 * version as the navigation target, count runs across all versions,
 * and pick the most-recently-updated run for the status pill.
 */
function buildRows(specs: readonly SpecSummary[], runs: readonly RunSummary[]): SpecGroupRow[] {
  // Map slug -> sorted-desc-by-version list of SpecSummary.
  const bySlug = new Map<string, SpecSummary[]>();
  for (const s of specs) {
    if (!bySlug.has(s.slug)) bySlug.set(s.slug, []);
    bySlug.get(s.slug)!.push(s);
  }
  for (const list of bySlug.values()) list.sort((a, b) => b.version - a.version);

  // Map fsm_spec_id -> slug (so we can attribute each run to its slug).
  const idToSlug = new Map<string, string>();
  for (const s of specs) idToSlug.set(s.id, s.slug);

  // Map slug -> { runs[] }; pick newest by last_update_at descending.
  const slugRuns = new Map<string, RunSummary[]>();
  for (const r of runs) {
    const slug = idToSlug.get(r.fsm_spec_id);
    if (!slug) continue;
    if (!slugRuns.has(slug)) slugRuns.set(slug, []);
    slugRuns.get(slug)!.push(r);
  }

  const rows: SpecGroupRow[] = [];
  for (const [slug, vList] of bySlug.entries()) {
    const runsForSlug = slugRuns.get(slug) ?? [];
    runsForSlug.sort((a, b) => (b.last_update_at ?? '').localeCompare(a.last_update_at ?? ''));
    rows.push({
      latest: vList[0],
      versionCount: vList.length,
      runCount: runsForSlug.length,
      newestRun: runsForSlug[0] ?? null,
    });
  }
  // Most-recently-active first: order by newest run's last_update_at
  // descending, with specs that have never been run falling to the
  // bottom but still sorted by latest-version registered_at.
  rows.sort((a, b) => {
    const aT = a.newestRun?.last_update_at ?? '';
    const bT = b.newestRun?.last_update_at ?? '';
    if (aT && !bT) return -1;
    if (bT && !aT) return 1;
    if (aT && bT) return bT.localeCompare(aT);
    return (b.latest.created_at ?? '').localeCompare(a.latest.created_at ?? '');
  });
  return rows;
}

function navigateTo(path: string): void {
  if (typeof window === 'undefined') return;
  window.history.pushState(null, '', path);
  window.dispatchEvent(new PopStateEvent('popstate'));
}

export function SpecsRoute(): JSX.Element {
  const [specs, setSpecs] = useState<SpecSummary[] | null>(null);
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    // The spec catalog needs to enumerate EVERY spec and EVERY run to
    // group + count per slug — derivative aggregations, not a
    // user-facing paged list — so we walk pages until ``has_next`` is
    // false (with a 50-page safety stop) instead of wiring a
    // <Pagination> control here.
    Promise.all([
      walkAllPages((p) => api.listSpecs(p), {}),
      walkAllPages((p) => api.listRuns(p), {}),
    ])
      .then(([s, r]) => {
        if (cancelled) return;
        setSpecs(s);
        setRuns(r);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof ApiError ? err.message : String(err));
      });
    return () => { cancelled = true; };
  }, []);

  const rows = useMemo(() => {
    if (!specs || !runs) return null;
    return buildRows(specs, runs);
  }, [specs, runs]);

  const cols: TableColumn<SpecGroupRow>[] = [
    {
      key: 'slug', label: 'Spec',
      render: (r) => (
        <div class="flex items-baseline gap-2">
          <span class="text-sm font-medium text-slate-900 dark:text-slate-100">{r.latest.slug}</span>
          <Pill variant="info" size="sm">v{r.latest.version}</Pill>
          {r.versionCount > 1 ? (
            <span class="text-[10px] text-slate-500 dark:text-slate-400">
              · {r.versionCount} versions
            </span>
          ) : null}
        </div>
      ),
    },
    {
      key: 'runs', label: 'Runs', align: 'right' as const, width: '5rem',
      render: (r) => (
        <span class="font-mono text-xs text-slate-700 dark:text-slate-300">{r.runCount}</span>
      ),
    },
    {
      key: 'status', label: 'Last run', width: '9rem',
      render: (r) => r.newestRun ? (
        <Pill variant={statusVariant(r.newestRun.status)} size="sm">{r.newestRun.status}</Pill>
      ) : (
        <span class="text-[10px] text-slate-500 dark:text-slate-400">no runs yet</span>
      ),
    },
    {
      key: 'lastUpdate', label: 'Last activity', width: '14rem',
      render: (r) => (
        <span class="text-xs text-slate-600 dark:text-slate-400">
          {formatTimestamp(r.newestRun?.last_update_at ?? r.latest.created_at)}
        </span>
      ),
    },
    {
      key: 'hash', label: 'Hash', width: '12rem',
      render: (r) => (
        <code class="font-mono text-xs text-slate-600 dark:text-slate-400" title={r.latest.hash}>
          {shortHash(r.latest.hash)}
        </code>
      ),
    },
  ];

  let body: JSX.Element;
  if (error) {
    body = <EmptyState title="Failed to load specs" message={error} />;
  } else if (rows === null) {
    body = (
      <div class="flex items-center justify-center py-12">
        <Spinner label="Loading specs and runs" />
      </div>
    );
  } else if (rows.length === 0) {
    body = (
      <EmptyState
        title="No specs registered"
        message="Register a spec via `ctxr-fsm spec register <module:attr>` then refresh."
      />
    );
  } else {
    body = (
      <Table<SpecGroupRow>
        columns={cols}
        rows={rows}
        rowKey={(r) => r.latest.slug}
        onRowClick={(r) => navigateTo(`/specs/${encodeURIComponent(r.latest.id)}`)}
        caption="Registered FSM specs, ordered by most recent activity"
      />
    );
  }

  return (
    <div class="p-4 md:p-6 space-y-4">
      <header>
        <h1 class="text-2xl font-semibold text-slate-900 dark:text-slate-100">Specs</h1>
        <p class="text-sm text-slate-600 dark:text-slate-400">
          The spec catalog is the entry point. Pick one to see its graph, runs, schemas, and version history.
          Ordered by most-recently-active first.
        </p>
      </header>
      <Card className="p-0">{body}</Card>
    </div>
  );
}

export default SpecsRoute;
