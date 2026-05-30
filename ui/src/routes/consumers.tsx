/**
 * /consumers — registered event-bus consumers.
 *
 * Renders the table returned by ``GET /api/v1/consumers`` so an operator
 * can see who is currently subscribed to the bus, what kinds they filter
 * on, whether they are scoped to a single run, and when they last
 * checked in.
 */

import type { JSX, VNode } from 'preact';
import { useEffect, useState } from 'preact/hooks';

import {
  Card,
  EmptyState,
  Pill,
  Spinner,
  Table,
  type TableColumn,
} from '../components';
import { api, ApiError, type Consumer } from '../lib/api';

/** Render an ISO timestamp as local-time text; ``null`` becomes a neutral pill. */
function formatLastSeen(iso: string | null): VNode {
  if (!iso) {
    return <Pill variant="neutral">never</Pill>;
  }
  let label = iso;
  try {
    const d = new Date(iso);
    if (!Number.isNaN(d.getTime())) label = d.toLocaleString();
  } catch {
    // fall through with raw iso
  }
  return <span class="text-slate-600 dark:text-slate-300">{label}</span>;
}

export function ConsumersRoute(): JSX.Element {
  const [consumers, setConsumers] = useState<Consumer[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .listConsumers()
      .then((rows) => {
        if (!cancelled) setConsumers(rows);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err instanceof ApiError ? err.message : String(err));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const columns: TableColumn<Consumer>[] = [
    {
      key: 'kind',
      label: 'Kind',
      width: '8rem',
      render: (row) => <Pill variant="info">{row.kind}</Pill>,
    },
    { key: 'name', label: 'Name' },
    {
      key: 'filter_kind',
      label: 'Filter (kind)',
      render: (row) =>
        row.filter_kind && row.filter_kind.length > 0 ? (
          <span class="flex flex-wrap gap-1">
            {row.filter_kind.map((k) => (
              <Pill key={k} variant="neutral">
                {k}
              </Pill>
            ))}
          </span>
        ) : (
          <span class="text-slate-400 dark:text-slate-500">—</span>
        ),
    },
    {
      key: 'filter_run_id',
      label: 'Filter (run)',
      width: '16rem',
      render: (row) =>
        row.filter_run_id ? (
          <code
            class="font-mono text-xs text-slate-600 dark:text-slate-300"
            title={row.filter_run_id}
          >
            {row.filter_run_id}
          </code>
        ) : (
          <span class="text-slate-400 dark:text-slate-500">—</span>
        ),
    },
    {
      key: 'last_seen_at',
      label: 'Last seen',
      width: '14rem',
      render: (row) => formatLastSeen(row.last_seen_at),
    },
  ];

  let body: VNode;
  if (error) {
    body = <EmptyState title="Failed to load consumers" message={error} />;
  } else if (consumers === null) {
    body = (
      <div class="flex items-center justify-center py-12">
        <Spinner label="Loading consumers" />
      </div>
    );
  } else {
    body = (
      <Table<Consumer>
        columns={columns}
        rows={consumers}
        rowKey={(row) => row.id}
        caption="Registered event-bus consumers"
        emptyState={
          <EmptyState
            title="No consumers registered"
            message="Register a consumer via POST /api/v1/consumers to see it here."
          />
        }
      />
    );
  }

  return (
    <div class="p-6 space-y-4">
      <header>
        <h1 class="text-2xl font-semibold">Consumers</h1>
        <p class="text-sm text-slate-600 dark:text-slate-400">
          Live view of every consumer currently attached to the FSM event bus.
        </p>
      </header>
      <Card className="p-0">{body}</Card>
    </div>
  );
}

export default ConsumersRoute;
