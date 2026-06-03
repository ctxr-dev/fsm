/**
 * Admin-sheet section: lock rows for this run.
 *
 * The /admin/locks endpoint returns every currently-held lock; this
 * section filters client-side to the rows whose run_id matches the
 * inspected run so an operator sees only the locks they can act on
 * from this surface.
 */

import type { JSX } from 'preact';
import { useEffect, useState } from 'preact/hooks';

import {
  EmptyState,
  Pill,
  Spinner,
} from '../../../components';
import { api, ApiError, type Lock } from '../../../lib/api';
import { CollapsibleSection } from './CollapsibleSection';

function formatTimestamp(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? (iso ?? '—') : d.toLocaleString();
}

export interface LockSectionProps {
  runId: string;
}

export function LockSection({ runId }: LockSectionProps): JSX.Element {
  const [locks, setLocks] = useState<Lock[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLocks(null);
    setError(null);
    api
      .listLocks({ page_size: 200 })
      .then((page) => {
        if (cancelled) return;
        setLocks(page.items.filter((l) => l.run_id === runId));
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err instanceof ApiError ? err.message : String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [runId]);

  const count = locks?.length ?? 0;
  const trailing =
    locks === null ? undefined : (
      <Pill variant="neutral" size="sm">
        {count}
      </Pill>
    );

  return (
    <CollapsibleSection id="admin-lock" title="Lock" trailing={trailing}>
      {error ? (
        <EmptyState title="Failed to load locks" message={error} />
      ) : locks === null ? (
        <Spinner label="Loading locks" />
      ) : locks.length === 0 ? (
        <p class="text-sm text-slate-500 dark:text-slate-400">
          No lock currently held.
        </p>
      ) : (
        <ul class="space-y-2">
          {locks.map((lock) => (
            <li
              key={`${lock.run_id}:${lock.holder_session_id}`}
              class="flex flex-wrap items-baseline gap-2 text-sm"
            >
              <Pill variant="info" size="sm">
                held
              </Pill>
              <code class="font-mono text-xs text-slate-600 dark:text-slate-300">
                {lock.holder_session_id}
              </code>
              <span class="text-xs text-slate-500 dark:text-slate-400">
                acquired {formatTimestamp(lock.acquired_at)}
              </span>
              <span class="text-xs text-slate-500 dark:text-slate-400">
                expires {formatTimestamp(lock.expires_at)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </CollapsibleSection>
  );
}

export default LockSection;
