/**
 * Admin-sheet section: commit-signature timeline for this run.
 *
 * The right-column Admin Card shows only the last 3 signatures; the
 * sheet surface trades a smaller header for room to scroll the full
 * history, so this section renders every row the server returned at
 * page_size=200 (the server-side MAX_PAGE_SIZE cap).
 */

import type { JSX } from 'preact';
import { useEffect, useRef, useState } from 'preact/hooks';

import {
  EmptyState,
  Pill,
  Spinner,
} from '../../../components';
import {
  api,
  ApiError,
  type CommitSignatureRecord,
} from '../../../lib/api';
import { signaturesRefreshNonce } from '../../../lib/runDetailRefresh';
import { CollapsibleSection } from './CollapsibleSection';

function shortHash(hash: string): string {
  return hash.length > 12 ? hash.slice(0, 12) : hash;
}

function formatTimestamp(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? (iso ?? '—') : d.toLocaleString();
}

export interface SignaturesSectionProps {
  runId: string;
}

export function SignaturesSection({
  runId,
}: SignaturesSectionProps): JSX.Element {
  const [sigs, setSigs] = useState<CommitSignatureRecord[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  // PR 6: SSE-driven refresh — refetch when the route bumps the
  // signatures nonce, keeping the previously-loaded list visible
  // across refetches so the panel does not flash empty on every tick.
  const nonce = signaturesRefreshNonce.value;
  const lastRunIdRef = useRef<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    if (lastRunIdRef.current !== runId) {
      setSigs(null);
      setError(null);
      lastRunIdRef.current = runId;
    }
    api
      .listCommitSignatures(runId, { page_size: 200 })
      .then((page) => {
        if (cancelled) return;
        setSigs(page.items);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err instanceof ApiError ? err.message : String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [runId, nonce]);

  const trailing = sigs ? (
    <Pill variant="neutral" size="sm">
      {sigs.length}
    </Pill>
  ) : undefined;

  return (
    <CollapsibleSection
      id="admin-signatures"
      title="Commit signatures"
      trailing={trailing}
    >
      {error ? (
        <EmptyState title="Failed to load signatures" message={error} />
      ) : sigs === null ? (
        <Spinner label="Loading signatures" />
      ) : sigs.length === 0 ? (
        <p class="text-sm text-slate-500 dark:text-slate-400">
          No commit signatures recorded yet.
        </p>
      ) : (
        <ul class="space-y-2">
          {sigs.map((sig) => (
            <li
              key={sig.id}
              class="text-xs text-slate-600 dark:text-slate-300 border border-slate-200 dark:border-slate-700 rounded-md px-3 py-2 space-y-1"
            >
              <div class="flex flex-wrap items-baseline gap-2">
                <code class="font-mono text-slate-700 dark:text-slate-200">
                  {sig.state_id}
                </code>
                <Pill
                  variant={sig.verified ? 'success' : 'danger'}
                  size="sm"
                >
                  {sig.verified ? 'verified' : 'unverified'}
                </Pill>
                {sig.iteration_n != null ? (
                  <span class="text-slate-500 dark:text-slate-400">
                    iter {sig.iteration_n}
                  </span>
                ) : null}
              </div>
              <div class="font-mono">
                sig{' '}
                <span title={sig.signature}>{shortHash(sig.signature)}</span>
              </div>
              <div class="text-slate-500 dark:text-slate-400">
                {formatTimestamp(sig.created_at)}
              </div>
            </li>
          ))}
        </ul>
      )}
    </CollapsibleSection>
  );
}

export default SignaturesSection;
