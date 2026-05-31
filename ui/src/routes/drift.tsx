/**
 * /drift — dashboard listing every run with drift signals.
 *
 * For each run with non-empty drift, shows: short id, current state,
 * status pill, score gauge, top signal_kind, signal count. Click row
 * → navigate to /runs/:id?focus=drift (the run detail's drift pane
 * is in W18d still in its right column; future iteration scrolls to
 * it).
 *
 * Data acquisition: listRuns then Promise.allSettled across
 * listDriftSignals per run. Capped at the first 50 runs to bound the
 * network cost.
 */

import type { JSX } from 'preact';
import { useEffect, useState } from 'preact/hooks';

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
  type DriftSignalsResponse,
  type RunSummary,
} from '../lib/api';

interface DriftRow {
  run: RunSummary;
  score: number;
  topKind: string | null;
  signalCount: number;
}

const RUN_CAP = 50;

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

export function DriftRoute(): JSX.Element {
  const [rows, setRows] = useState<DriftRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const runs = await api.listRuns({});
        const capped = runs.slice(0, RUN_CAP);
        const settled = await Promise.allSettled(
          capped.map((r) =>
            api
              .listDriftSignals(r.id)
              .then((d): { run: RunSummary; resp: DriftSignalsResponse } => ({ run: r, resp: d })),
          ),
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
  }, []);

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
          <EmptyState title="No drift signals" message={`Across the last ${RUN_CAP} runs, none accumulated any drift signals.`} />
        ) : (
          <Table<DriftRow>
            columns={cols}
            rows={rows}
            rowKey={(r) => r.run.id}
            caption="Drift across runs"
          />
        )}
      </Card>
    </div>
  );
}

export default DriftRoute;
