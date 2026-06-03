/**
 * Admin-sheet section: journal txns for this run.
 *
 * The `/admin/journal_txns` endpoint does not yet accept a ``run_id``
 * filter (see ``ListJournalTxnsParams`` in lib/api.ts — only ``status``
 * is plumbed through). We page through a generously-sized response and
 * filter client-side so the section can render a run-scoped subset
 * today without waiting on a server change. Volume is bounded by the
 * MAX_PAGE_SIZE cap on the server (200).
 *
 * Each row mirrors the inline journal block in the existing
 * right-column Admin Card (status Pill, staged-write count, started_at
 * timestamp) plus a JsonViewer for the staged writes payload so an
 * operator can inspect what the engine is about to replay.
 */

import type { JSX } from 'preact';
import { useEffect, useState } from 'preact/hooks';

import {
  EmptyState,
  JsonViewer,
  Pill,
  Spinner,
  type PillVariant,
} from '../../../components';
import { api, ApiError, type JournalTxn } from '../../../lib/api';
import { CollapsibleSection } from './CollapsibleSection';

const JOURNAL_VARIANTS: Record<string, PillVariant> = {
  pending: 'warning',
  ready_to_finalise: 'info',
  finalised: 'success',
};

function variantForJournal(status: string): PillVariant {
  return JOURNAL_VARIANTS[status.toLowerCase()] ?? 'neutral';
}

function formatTimestamp(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? (iso ?? '—') : d.toLocaleString();
}

export interface JournalSectionProps {
  runId: string;
}

export function JournalSection({ runId }: JournalSectionProps): JSX.Element {
  const [txns, setTxns] = useState<JournalTxn[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setTxns(null);
    setError(null);
    api
      .listJournalTxns({ page_size: 200 })
      .then((page) => {
        if (cancelled) return;
        setTxns(page.items.filter((t) => t.run_id === runId));
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err instanceof ApiError ? err.message : String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [runId]);

  const count = txns?.length ?? 0;
  const trailing =
    txns === null ? undefined : (
      <Pill variant="neutral" size="sm">
        {count}
      </Pill>
    );

  return (
    <CollapsibleSection
      id="admin-journal"
      title="Journal"
      trailing={trailing}
      defaultOpen={true}
    >
      {error ? (
        <EmptyState title="Failed to load journal" message={error} />
      ) : txns === null ? (
        <Spinner label="Loading journal" />
      ) : txns.length === 0 ? (
        <p class="text-sm text-slate-500 dark:text-slate-400">
          No journal txns recorded for this run.
        </p>
      ) : (
        <ul class="space-y-3">
          {txns.map((txn) => (
            <li
              key={txn.id}
              class="rounded-md border border-slate-200 dark:border-slate-700 px-3 py-2 space-y-1"
            >
              <div class="flex flex-wrap items-baseline gap-2">
                <Pill variant={variantForJournal(txn.status)} size="sm">
                  {txn.status}
                </Pill>
                <span class="text-xs font-mono text-slate-500 dark:text-slate-400">
                  {txn.staged_writes.length} staged write
                  {txn.staged_writes.length === 1 ? '' : 's'}
                </span>
                <span class="text-xs text-slate-500 dark:text-slate-400">
                  started {formatTimestamp(txn.started_at)}
                </span>
              </div>
              <JsonViewer
                value={{ staged_writes: txn.staged_writes }}
                rootLabel="staged_writes"
                mode="inline"
                maxInlineHeight="max-h-40"
                ariaLabel={`Staged writes for journal txn ${txn.id}`}
              />
            </li>
          ))}
        </ul>
      )}
    </CollapsibleSection>
  );
}

export default JournalSection;
