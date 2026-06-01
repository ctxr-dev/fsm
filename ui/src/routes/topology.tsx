/**
 * /topology — event-bus topology view (replaces /consumers).
 *
 * Two panels: Producers (left) and Consumers (right) plus an SSE
 * health strip. Each producer / consumer row exposes a click target
 * that copies its id and surfaces last_seen_at where available.
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
  type TableColumn,
} from '../components';
import {
  api,
  ApiError,
  type Consumer,
  type Page,
  type Producer,
} from '../lib/api';

function fmt(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

const DEFAULT_PAGE_SIZE = 50;

export function TopologyRoute(): JSX.Element {
  const [producers, setProducers] = useState<Page<Producer> | null>(null);
  const [consumers, setConsumers] = useState<Page<Consumer> | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [producerPage, setProducerPage] = useState(1);
  const [producerPageSize, setProducerPageSize] = useState(DEFAULT_PAGE_SIZE);
  const [consumerPage, setConsumerPage] = useState(1);
  const [consumerPageSize, setConsumerPageSize] = useState(DEFAULT_PAGE_SIZE);

  useEffect(() => {
    let cancelled = false;
    api.listProducers({ page: producerPage, page_size: producerPageSize })
      .then((p) => {
        if (!cancelled) setProducers(p);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof ApiError ? err.message : String(err));
      });
    return () => { cancelled = true; };
  }, [producerPage, producerPageSize]);

  useEffect(() => {
    let cancelled = false;
    api.listConsumers({ page: consumerPage, page_size: consumerPageSize })
      .then((c) => {
        if (!cancelled) setConsumers(c);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof ApiError ? err.message : String(err));
      });
    return () => { cancelled = true; };
  }, [consumerPage, consumerPageSize]);

  const producerCols: TableColumn<Producer>[] = [
    { key: 'kind', label: 'Kind', render: (r) => <Pill variant="info" size="sm">{r.kind}</Pill> },
    { key: 'name', label: 'Name' },
    { key: 'created_at', label: 'Registered', render: (r) => <span class="text-xs text-slate-500">{fmt(r.created_at)}</span> },
  ];

  const consumerCols: TableColumn<Consumer>[] = [
    { key: 'kind', label: 'Kind', render: (r) => <Pill variant="info" size="sm">{r.kind}</Pill> },
    { key: 'name', label: 'Name' },
    {
      key: 'filter_kind',
      label: 'Filter kinds',
      render: (r) => (
        r.filter_kind && r.filter_kind.length > 0 ? (
          <div class="flex flex-wrap gap-1">
            {r.filter_kind.map((k) => (
              <Pill key={k} variant="neutral" size="sm">{k}</Pill>
            ))}
          </div>
        ) : (
          <span class="text-xs text-slate-400">all</span>
        )
      ),
    },
    { key: 'filter_run_id', label: 'Run', render: (r) => r.filter_run_id ? <code class="text-xs">{r.filter_run_id.slice(0, 7)}</code> : <span class="text-xs text-slate-400">any</span> },
    { key: 'last_seen_at', label: 'Last seen', render: (r) => <span class="text-xs text-slate-500">{fmt(r.last_seen_at)}</span> },
  ];

  if (error) {
    return (
      <div class="p-4 md:p-6">
        <Card>
          <EmptyState title="Failed to load topology" message={error} />
        </Card>
      </div>
    );
  }

  return (
    <div class="p-4 md:p-6 space-y-4">
      <header>
        <h1 class="text-2xl font-semibold">Topology</h1>
        <p class="text-sm text-slate-600 dark:text-slate-400">
          Producers emit FSM events; consumers subscribe with filters. This is the live wiring diagram.
        </p>
      </header>
      <div class="grid gap-4 lg:grid-cols-2">
        <Card title={`Producers${producers ? ` (${producers.total})` : ''}`} className="p-0">
          {producers === null ? (
            <div class="flex items-center justify-center py-12"><Spinner label="Loading producers" /></div>
          ) : producers.items.length === 0 ? (
            <EmptyState title="No producers" message="No producer has registered yet." />
          ) : (
            <>
              <Table<Producer>
                columns={producerCols}
                rows={producers.items}
                rowKey={(r) => r.id}
                caption="Registered event producers"
              />
              <div class="border-t border-slate-200 px-4 py-3 dark:border-slate-700">
                <Pagination
                  page={producers}
                  onPageChange={(p) => setProducerPage(p)}
                  onPageSizeChange={(sz) => { setProducerPageSize(sz); setProducerPage(1); }}
                  itemLabel="producers"
                />
              </div>
            </>
          )}
        </Card>
        <Card title={`Consumers${consumers ? ` (${consumers.total})` : ''}`} className="p-0">
          {consumers === null ? (
            <div class="flex items-center justify-center py-12"><Spinner label="Loading consumers" /></div>
          ) : consumers.items.length === 0 ? (
            <EmptyState title="No consumers" message="No consumer has registered yet." />
          ) : (
            <>
              <Table<Consumer>
                columns={consumerCols}
                rows={consumers.items}
                rowKey={(r) => r.id}
                caption="Registered event consumers"
              />
              <div class="border-t border-slate-200 px-4 py-3 dark:border-slate-700">
                <Pagination
                  page={consumers}
                  onPageChange={(p) => setConsumerPage(p)}
                  onPageSizeChange={(sz) => { setConsumerPageSize(sz); setConsumerPage(1); }}
                  itemLabel="consumers"
                />
              </div>
            </>
          )}
        </Card>
      </div>
    </div>
  );
}

export default TopologyRoute;
