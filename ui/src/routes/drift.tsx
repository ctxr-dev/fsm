/**
 * /drift — dashboard listing every run with drift signals.
 *
 * For each run with non-empty drift, shows: short id, current state,
 * status pill, score gauge, top signal_kind, signal count. Click row
 * → navigate to /runs/:id?focus=drift (the run detail's drift pane
 * is in W18d still in its right column; future iteration scrolls to
 * it).
 *
 * Data acquisition: listRuns (paginated) then Promise.allSettled
 * across listDriftSignals per run in the current page. The user
 * navigates pages via the <Pagination> control at the bottom.
 */

import type { JSX } from 'preact';
import { useEffect, useState } from 'preact/hooks';

import {
  Card,
  EmptyState,
  Pagination,
  Pill,
  Spinner,
  Table,
  type PillVariant,
  type TableColumn,
} from '../components';
import {
  api,
  ApiError,
  type DriftSignalsResponse,
  type Page,
  type RunSummary,
} from '../lib/api';

interface DriftRow {
  run: RunSummary;
  score: number;
  topKind: string | null;
  signalCount: number;
}

const DEFAULT_PAGE_SIZE = 50;

function scoreVariant(s: number): PillVariant {
  if (s >= 0.7) return 'danger';
  if (s >= 0.4) return 'warning';
  return 'success';
}

function statusVariant(status: string): PillVariant {
  switch (status) {
    case 'completed': return 'success';
    case 'paused': return 'warning';
    case 'faulted':
    case 'aborted':
    case 'drift_paused': return 'danger';
    case 'in_progress': return 'info';
    default: return 'neutral';
  }
}

/**
 * Maximum concurrent ``listDriftSignals`` requests in flight at any
 * moment while seeding the drift table. The fan-out is bounded
 * because (a) the browser's per-host connection limit serialises
 * excess requests anyway, so launching 200 at once just wastes the
 * scheduler's time, and (b) each call exercises a SQL query against
 * a different run — at 200 parallel reads SQLite's WAL still cooperates
 * but the API process eats the CPU for no UX gain. ``DRIFT_FANOUT_LIMIT``
 * matches the pre-W22b2-iter hard-coded ``RUN_CAP=50`` so the worst-
 * case load on the API stays bounded regardless of which page size the
 * operator picks. A future per-API-call multi-run endpoint would let
 * us drop this entirely.
 */
const DRIFT_FANOUT_LIMIT = 50;

/**
 * Fire ``fn`` against each input with at most ``limit`` calls in
 * flight. Returns ``PromiseSettledResult[]`` in input order so callers
 * can mix fulfilled / rejected outcomes without paying a separate
 * try / catch per item. Implemented inline rather than pulled in as
 * ``p-limit`` because the dependency would be ~2 KB gzipped for one
 * call site; if a second consumer surfaces we lift this to a lib.
 */
async function mapLimited<T, R>(
  items: readonly T[],
  limit: number,
  fn: (item: T) => Promise<R>,
): Promise<PromiseSettledResult<R>[]> {
  const out = new Array<PromiseSettledResult<R>>(items.length);
  let cursor = 0;
  const workers = Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (true) {
      const idx = cursor;
      cursor += 1;
      if (idx >= items.length) return;
      try {
        const value = await fn(items[idx]);
        out[idx] = { status: 'fulfilled', value };
      } catch (reason) {
        out[idx] = { status: 'rejected', reason };
      }
    }
  });
  await Promise.all(workers);
  return out;
}

export function DriftRoute(): JSX.Element {
  const [runsPage, setRunsPage] = useState<Page<RunSummary> | null>(null);
  const [rows, setRows] = useState<DriftRow[] | null>(null);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setRows(null);
    setRunsPage(null);
    (async () => {
      try {
        const runs = await api.listRuns({ page, page_size: pageSize });
        if (cancelled) return;
        setRunsPage(runs);
        const settled = await mapLimited(
          runs.items,
          DRIFT_FANOUT_LIMIT,
          (r) =>
            api
              .listDriftSignals(r.id)
              .then((d): { run: RunSummary; resp: DriftSignalsResponse } => ({ run: r, resp: d })),
        );
        const out: DriftRow[] = [];
        for (const s of settled) {
          if (s.status !== 'fulfilled') continue;
          const { run, resp } = s.value;
          if (!resp.signals || resp.signals.length === 0) continue;
          const counts = new Map<string, number>();
          for (const sig of resp.signals) counts.set(sig.signal_kind, (counts.get(sig.signal_kind) ?? 0) + 1);
          const top = [...counts.entries()].sort((a, b) => b[1] - a[1])[0];
          out.push({
            run,
            score: resp.score ?? 0,
            topKind: top ? top[0] : null,
            signalCount: resp.signals.length,
          });
        }
        out.sort((a, b) => b.score - a.score);
        if (!cancelled) setRows(out);
      } catch (err) {
        if (!cancelled) setError(err instanceof ApiError ? err.message : String(err));
      }
    })();
    return () => { cancelled = true; };
  }, [page, pageSize]);

  const cols: TableColumn<DriftRow>[] = [
    {
      key: 'run', label: 'Run', render: (r) => (
        <a href={`/runs/${r.run.id}`} class="font-mono text-xs hover:underline">{r.run.id.slice(0, 7)}</a>
      ),
    },
    {
      key: 'status', label: 'Status',
      render: (r) => <Pill variant={statusVariant(r.run.status)} size="sm">{r.run.status}</Pill>,
    },
    { key: 'current_state', label: 'Current', render: (r) => <code class="text-xs">{r.run.current_state ?? '—'}</code> },
    {
      key: 'score', label: 'Score',
      render: (r) => <Pill variant={scoreVariant(r.score)}>{r.score.toFixed(2)}</Pill>,
    },
    { key: 'topKind', label: 'Top signal', render: (r) => <span class="text-xs">{r.topKind ?? '—'}</span> },
    { key: 'signalCount', label: '# signals', align: 'right' as const, render: (r) => <span class="text-xs">{r.signalCount}</span> },
  ];

  if (error) {
    return (
      <div class="p-4 md:p-6">
        <Card>
          <EmptyState title="Failed to compute drift" message={error} />
        </Card>
      </div>
    );
  }

  return (
    <div class="p-4 md:p-6 space-y-4">
      <header>
        <h1 class="text-2xl font-semibold">Drift</h1>
        <p class="text-sm text-slate-600 dark:text-slate-400">
          Runs ranked by accumulated drift score (W12). Threshold 0.7 triggers auto-pause.
        </p>
      </header>
      <Card className="p-0">
        {rows === null ? (
          <div class="flex items-center justify-center py-12"><Spinner label="Computing drift across runs" /></div>
        ) : rows.length === 0 ? (
          <EmptyState
            title="No drift signals"
            message={`Across the ${runsPage?.items.length ?? 0} run(s) on this page, none accumulated any drift signals.`}
          />
        ) : (
          <Table<DriftRow>
            columns={cols}
            rows={rows}
            rowKey={(r) => r.run.id}
            caption="Drift across runs"
          />
        )}
      </Card>
      {runsPage !== null && runsPage.total > 0 ? (
        <Pagination
          page={runsPage}
          onPageChange={(p) => setPage(p)}
          onPageSizeChange={(sz) => { setPageSize(sz); setPage(1); }}
          itemLabel="runs"
        />
      ) : null}
    </div>
  );
}

export default DriftRoute;
