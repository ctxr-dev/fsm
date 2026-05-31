/**
 * /specs — registered FSM specs.
 *
 * Lists every spec returned by ``GET /api/v1/specs``; clicking a row
 * opens a Dialog with the full ``FsmSpec`` JSON fetched lazily via
 * ``GET /api/v1/specs/{id}``.
 */

import type { JSX, VNode } from 'preact';
import { useEffect, useState } from 'preact/hooks';

import {
  Card, Dialog, EmptyState, Pill, Spinner, Table, type TableColumn,
} from '../components';
import { api, ApiError, type SpecDetail, type SpecSummary } from '../lib/api';

const shortHash = (h: string): string => (h.length > 12 ? h.slice(0, 12) : h);

function formatTimestamp(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

export function SpecsRoute(): JSX.Element {
  const [specs, setSpecs] = useState<SpecSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<SpecDetail | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.listSpecs()
      .then((rows) => { if (!cancelled) setSpecs(rows); })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof ApiError ? err.message : String(err));
      });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (!selectedId) { setDetail(null); setDetailError(null); return; }
    let cancelled = false;
    setDetail(null); setDetailError(null);
    api.getSpec(selectedId)
      .then((d) => { if (!cancelled) setDetail(d); })
      .catch((err: unknown) => {
        if (!cancelled) setDetailError(err instanceof ApiError ? err.message : String(err));
      });
    return () => { cancelled = true; };
  }, [selectedId]);

  const columns: TableColumn<SpecSummary>[] = [
    { key: 'slug', label: 'Slug' },
    {
      key: 'version', label: 'Version', align: 'right', width: '6rem',
      render: (row) => <Pill variant="info">v{row.version}</Pill>,
    },
    {
      key: 'hash', label: 'Hash', width: '12rem',
      render: (row) => (
        <code class="font-mono text-xs text-slate-600 dark:text-slate-300" title={row.hash}>
          {shortHash(row.hash)}
        </code>
      ),
    },
    {
      key: 'created_at', label: 'Registered', width: '14rem',
      render: (row) => (
        <span class="text-slate-600 dark:text-slate-300">{formatTimestamp(row.created_at)}</span>
      ),
    },
  ];

  let body: VNode;
  if (error) {
    body = <EmptyState title="Failed to load specs" message={error} />;
  } else if (specs === null) {
    body = <div class="flex items-center justify-center py-12"><Spinner label="Loading specs" /></div>;
  } else {
    body = (
      <Table<SpecSummary>
        columns={columns} rows={specs} rowKey={(row) => row.id}
        onRowClick={(row) => setSelectedId(row.id)}
        caption="Registered FSM specs"
        emptyState={<EmptyState title="No specs registered" message="Register an FSM spec via POST /api/v1/specs." />}
      />
    );
  }

  return (
    <div class="p-6 space-y-4">
      <header>
        <h1 class="text-2xl font-semibold">Specs</h1>
        <p class="text-sm text-slate-600 dark:text-slate-400">
          Registered FSM definitions. Click a row to view its JSON.
        </p>
      </header>
      <Card className="p-0">{body}</Card>
      <Dialog
        open={selectedId !== null}
        onClose={() => setSelectedId(null)}
        title={detail ? `${detail.slug} v${detail.version}` : 'Spec'}
        widthClassName="max-w-3xl"
      >
        {detailError ? (
          <p class="text-sm text-red-700 dark:text-red-300">{detailError}</p>
        ) : !detail ? (
          <div class="flex items-center justify-center py-8"><Spinner label="Loading spec" /></div>
        ) : (
          <pre class="font-mono text-xs leading-relaxed whitespace-pre-wrap break-words text-slate-800 dark:text-slate-200">
            {JSON.stringify(detail.definition, null, 2)}
          </pre>
        )}
      </Dialog>
    </div>
  );
}

export default SpecsRoute;
