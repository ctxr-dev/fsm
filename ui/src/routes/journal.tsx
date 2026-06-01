/**
 * /journal — recovery wizard for pending / ready journal txns.
 *
 * Lists every txn whose status != 'finalised', grouped by run_id.
 * Each txn row exposes its staged_writes via a JsonViewer + Discard
 * / Replay buttons gated on status. Recovery posts through
 * `api.recoverJournal(runId, action)`.
 */

import type { JSX } from 'preact';
import { useCallback, useEffect, useMemo, useState } from 'preact/hooks';

import {
  Button,
  Card,
  Dialog,
  EmptyState,
  JsonViewer,
  Pagination,
  Pill,
  Spinner,
  Timeline,
  useToast,
  type PillVariant,
  type TimelineItem,
} from '../components';
import { api, ApiError, type JournalTxn, type Page } from '../lib/api';

const DEFAULT_PAGE_SIZE = 200;

function fmt(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

function statusVariant(s: string): PillVariant {
  switch (s) {
    case 'finalised': return 'success';
    case 'ready_to_finalise': return 'warning';
    case 'pending': return 'info';
    default: return 'neutral';
  }
}

interface Pending {
  runId: string;
  action: 'discard' | 'replay';
  txnId: string;
}

export function JournalRoute(): JSX.Element {
  const [txnsPage, setTxnsPage] = useState<Page<JournalTxn> | null>(null);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<Pending | null>(null);
  const [busy, setBusy] = useState(false);
  const toast = useToast();

  const reload = useCallback(() => {
    let cancelled = false;
    setTxnsPage(null);
    api.listJournalTxns({ page, page_size: pageSize })
      .then((resp) => {
        if (!cancelled) {
          // Sort recent first by started_at within the page.
          const sortedItems = [...resp.items].sort((a, b) =>
            (b.started_at ?? '').localeCompare(a.started_at ?? ''),
          );
          setTxnsPage({ ...resp, items: sortedItems });
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof ApiError ? err.message : String(err));
      });
    return () => { cancelled = true; };
  }, [page, pageSize]);

  useEffect(reload, [reload]);

  const txns = txnsPage?.items ?? null;

  const grouped = useMemo(() => {
    if (!txns) return [];
    const map = new Map<string, JournalTxn[]>();
    for (const t of txns) {
      if (t.status === 'finalised') continue; // wizard's job is non-finalised only
      if (!map.has(t.run_id)) map.set(t.run_id, []);
      map.get(t.run_id)!.push(t);
    }
    return [...map.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  }, [txns]);

  const performAction = useCallback(async () => {
    if (!pending) return;
    setBusy(true);
    try {
      await api.recoverJournal(pending.runId, pending.action);
      toast.success(`Journal txn ${pending.action}ed`);
      setPending(null);
      reload();
    } catch (err) {
      toast.danger(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [pending, reload, toast]);

  const items = useMemo<TimelineItem[]>(
    () =>
      (txns ?? [])
        .filter((t) => t.status !== 'finalised')
        .map((t) => ({
          id: t.id,
          timestamp: t.started_at,
          title: `txn ${t.id.slice(0, 7)} (run ${t.run_id.slice(0, 7)})`,
          kind: t.status,
          variant: statusVariant(t.status),
          payload: (
            <div class="space-y-2">
              <div class="flex flex-wrap gap-2 items-center text-xs">
                <span>started {fmt(t.started_at)}</span>
                <span>ready {fmt(t.ready_at)}</span>
                <span>{t.staged_writes?.length ?? 0} staged write(s)</span>
              </div>
              <JsonViewer
                value={t.staged_writes}
                rootLabel="staged_writes"
                mode="inline"
                maxInlineHeight="max-h-40"
              />
              <div class="flex gap-2">
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => setPending({ runId: t.run_id, action: 'replay', txnId: t.id })}
                  disabled={t.status === 'pending'}
                  title={t.status === 'pending' ? 'Replay only valid for ready_to_finalise' : 'Replay'}
                >
                  Replay
                </Button>
                <Button
                  variant="danger"
                  size="sm"
                  onClick={() => setPending({ runId: t.run_id, action: 'discard', txnId: t.id })}
                >
                  Discard
                </Button>
              </div>
            </div>
          ),
        })),
    [txns],
  );

  if (error) {
    return (
      <div class="p-4 md:p-6">
        <Card>
          <EmptyState title="Failed to load journal txns" message={error} />
        </Card>
      </div>
    );
  }

  return (
    <div class="p-4 md:p-6 space-y-4">
      <header class="flex items-baseline justify-between gap-2">
        <div>
          <h1 class="text-2xl font-semibold">Journal recovery</h1>
          <p class="text-sm text-slate-600 dark:text-slate-400">
            Pending + ready journal txns across all runs. Replay or discard from here.
          </p>
        </div>
        <Button variant="ghost" size="sm" onClick={reload}>Refresh</Button>
      </header>
      <Card>
        {txnsPage === null ? (
          <div class="flex items-center justify-center py-12"><Spinner label="Loading journal" /></div>
        ) : grouped.length === 0 ? (
          <EmptyState title="No pending txns" message="Every journal txn across every run is finalised." />
        ) : (
          <div class="space-y-4">
            {grouped.map(([runId, runTxns]) => (
              <div key={runId}>
                <div class="flex items-center gap-2 mb-1 text-sm">
                  <a href={`/runs/${runId}`} class="font-mono text-xs hover:underline">
                    run {runId.slice(0, 7)}
                  </a>
                  <Pill variant="info" size="sm">{runTxns.length} txn{runTxns.length === 1 ? '' : 's'}</Pill>
                </div>
                <Timeline
                  items={items.filter((i) => runTxns.some((t) => t.id === i.id))}
                  label={`Journal txns for run ${runId.slice(0, 7)}`}
                />
              </div>
            ))}
          </div>
        )}
      </Card>
      {txnsPage !== null && txnsPage.total > 0 ? (
        <Pagination
          page={txnsPage}
          onPageChange={(p) => setPage(p)}
          onPageSizeChange={(sz) => { setPageSize(sz); setPage(1); }}
          itemLabel="journal txns"
        />
      ) : null}
      <Dialog
        open={pending !== null}
        onClose={() => setPending(null)}
        title={pending ? `${pending.action === 'discard' ? 'Discard' : 'Replay'} journal txn` : ''}
        widthClassName="max-w-md"
        footer={
          <>
            <Button variant="ghost" onClick={() => setPending(null)} disabled={busy}>Cancel</Button>
            <Button variant={pending?.action === 'discard' ? 'danger' : 'primary'} onClick={performAction} loading={busy}>
              {pending?.action === 'discard' ? 'Discard' : 'Replay'}
            </Button>
          </>
        }
      >
        {pending ? (
          <p class="text-sm">
            {pending.action === 'discard'
              ? `Drop the staged writes for txn ${pending.txnId.slice(0, 7)}? The run continues from its last finalised state.`
              : `Replay the staged writes for txn ${pending.txnId.slice(0, 7)} and finalise the txn?`}
          </p>
        ) : null}
      </Dialog>
    </div>
  );
}

export default JournalRoute;
