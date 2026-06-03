/**
 * Admin-sheet section: tool-call audit log for this run.
 *
 * Mirrors the bottom collapsible "Tool calls audit log" on /runs/:id
 * with two extras suited to the sheet surface:
 *
 *   1. **Local filter chips** for ``ok`` / ``failed`` status (toggle by
 *      tap; cleared by re-tapping). Independent of the page-wide
 *      runDetailFilters signal so opening the sheet doesn't surprise
 *      the operator's existing filter set on the main view.
 *   2. **Per-row JsonViewer** for ``args_redacted`` so the operator can
 *      inspect what the producer actually called the tool with.
 */

import type { JSX } from 'preact';
import { useEffect, useMemo, useRef, useState } from 'preact/hooks';

import {
  EmptyState,
  FilterChips,
  JsonViewer,
  Pill,
  Spinner,
  type FilterChip,
} from '../../../components';
import { api, ApiError, type ToolCall } from '../../../lib/api';
import { toolCallsRefreshNonce } from '../../../lib/runDetailRefresh';
import { CollapsibleSection } from './CollapsibleSection';

function shortHash(hash: string): string {
  return hash.length > 12 ? hash.slice(0, 12) : hash;
}

function formatTimestamp(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? (iso ?? '—') : d.toLocaleString();
}

type StatusFilter = 'all' | 'ok' | 'failed';

export interface ToolCallsSectionProps {
  runId: string;
}

export function ToolCallsSection({
  runId,
}: ToolCallsSectionProps): JSX.Element {
  const [calls, setCalls] = useState<ToolCall[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all');
  // PR 6: refetch on SSE-driven tool-calls nonce bumps.
  const nonce = toolCallsRefreshNonce.value;
  const lastRunIdRef = useRef<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    if (lastRunIdRef.current !== runId) {
      setCalls(null);
      setError(null);
      lastRunIdRef.current = runId;
    }
    api
      .listToolCalls({ run_id: runId, page_size: 100 })
      .then((page) => {
        if (cancelled) return;
        setCalls(page.items);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err instanceof ApiError ? err.message : String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [runId, nonce]);

  const filtered = useMemo(() => {
    if (!calls) return [];
    if (statusFilter === 'all') return calls;
    return calls.filter((c) =>
      statusFilter === 'ok' ? c.succeeded : !c.succeeded,
    );
  }, [calls, statusFilter]);

  const chips: FilterChip[] = useMemo(() => {
    if (statusFilter === 'all') return [];
    return [
      {
        id: `status:${statusFilter}`,
        kind: 'status',
        label: `status: ${statusFilter}`,
      },
    ];
  }, [statusFilter]);

  const trailing = calls ? (
    <Pill variant="neutral" size="sm">
      {filtered.length === calls.length
        ? filtered.length
        : `${filtered.length}/${calls.length}`}
    </Pill>
  ) : undefined;

  return (
    <CollapsibleSection
      id="admin-tool-calls"
      title="Tool calls"
      trailing={trailing}
    >
      {error ? (
        <EmptyState title="Failed to load tool calls" message={error} />
      ) : calls === null ? (
        <Spinner label="Loading tool calls" />
      ) : (
        <div class="space-y-3">
          <div class="flex flex-wrap items-center gap-2">
            <button
              type="button"
              aria-pressed={statusFilter === 'ok'}
              onClick={() =>
                setStatusFilter((v) => (v === 'ok' ? 'all' : 'ok'))
              }
              class={[
                'inline-flex items-center px-2 py-0.5 text-xs rounded-md border',
                'focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500',
                statusFilter === 'ok'
                  ? 'bg-emerald-100 border-emerald-300 text-emerald-800 dark:bg-emerald-900/40 dark:border-emerald-700 dark:text-emerald-200'
                  : 'border-slate-300 text-slate-600 dark:border-slate-600 dark:text-slate-300',
              ].join(' ')}
            >
              ok
            </button>
            <button
              type="button"
              aria-pressed={statusFilter === 'failed'}
              onClick={() =>
                setStatusFilter((v) => (v === 'failed' ? 'all' : 'failed'))
              }
              class={[
                'inline-flex items-center px-2 py-0.5 text-xs rounded-md border',
                'focus:outline-none focus-visible:ring-2 focus-visible:ring-red-500',
                statusFilter === 'failed'
                  ? 'bg-red-100 border-red-300 text-red-800 dark:bg-red-900/40 dark:border-red-700 dark:text-red-200'
                  : 'border-slate-300 text-slate-600 dark:border-slate-600 dark:text-slate-300',
              ].join(' ')}
            >
              failed
            </button>
            <FilterChips
              chips={chips}
              onRemove={() => setStatusFilter('all')}
              onClear={() => setStatusFilter('all')}
              ariaLabel="Tool call filters"
            />
          </div>
          {filtered.length === 0 ? (
            <EmptyState
              title="No tool calls"
              message={
                calls.length === 0
                  ? 'The audit log is empty for this run.'
                  : 'No tool calls match the current filter.'
              }
            />
          ) : (
            <ul class="divide-y divide-slate-200 dark:divide-slate-700">
              {filtered.map((call) => (
                <li key={call.id} class="py-3 space-y-1">
                  <div class="flex flex-wrap items-baseline gap-2">
                    <Pill
                      variant={call.succeeded ? 'success' : 'danger'}
                      size="sm"
                    >
                      {call.succeeded ? 'ok' : 'failed'}
                    </Pill>
                    <code class="font-mono text-sm text-slate-800 dark:text-slate-100">
                      {call.tool_name}
                    </code>
                    <span class="text-xs text-slate-500 dark:text-slate-400">
                      {formatTimestamp(call.created_at)}
                    </span>
                    <span
                      class="text-xs font-mono text-slate-400 dark:text-slate-500"
                      title={call.producer_id}
                    >
                      {shortHash(call.producer_id)}
                    </span>
                  </div>
                  <JsonViewer
                    value={call.args_redacted}
                    rootLabel="args"
                    mode="inline"
                    maxInlineHeight="max-h-40"
                    ariaLabel={`Tool call args ${call.tool_name}`}
                  />
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </CollapsibleSection>
  );
}

export default ToolCallsSection;
