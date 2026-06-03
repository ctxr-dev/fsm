/**
 * Admin-sheet section: drift score + individual signals for this run.
 *
 * Mirrors the gauge already rendered in the right-column Admin Card on
 * /runs/:id, plus a list of the underlying signals so an operator can
 * see WHICH ones contributed when the aggregate score crosses the
 * danger threshold. The gauge lives here as a local helper (not yet
 * exported from components/) so this section is self-contained until
 * PR 1's shared DriftGauge lands on this branch.
 */

import type { JSX } from 'preact';
import { useEffect, useState } from 'preact/hooks';

import {
  EmptyState,
  Pill,
  Spinner,
} from '../../../components';
import { api, ApiError, type DriftSignalsResponse } from '../../../lib/api';
import { CollapsibleSection } from './CollapsibleSection';

/** Visual gauge for a drift score in the ``[0, 1]`` band. */
function DriftGauge({ score }: { score: number }): JSX.Element {
  const clamped = Math.max(0, Math.min(1, score));
  const pct = Math.round(clamped * 100);
  const tone =
    clamped >= 0.7
      ? 'bg-red-500'
      : clamped >= 0.4
      ? 'bg-amber-500'
      : 'bg-emerald-500';
  return (
    <div class="space-y-1">
      <div class="flex items-baseline justify-between text-xs text-slate-500 dark:text-slate-400">
        <span>Drift score</span>
        <span class="font-mono text-slate-700 dark:text-slate-200">
          {clamped.toFixed(3)}
        </span>
      </div>
      <div
        class="h-2 w-full rounded-full bg-slate-200 dark:bg-slate-700 overflow-hidden"
        role="progressbar"
        aria-label="Drift score"
        aria-valuemin={0}
        aria-valuemax={1}
        aria-valuenow={clamped}
      >
        <div
          class={`h-full ${tone} motion-safe:transition-[width] motion-safe:duration-300`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

export interface DriftSectionProps {
  runId: string;
}

export function DriftSection({ runId }: DriftSectionProps): JSX.Element {
  const [drift, setDrift] = useState<DriftSignalsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState<boolean>(false);

  useEffect(() => {
    let cancelled = false;
    setDrift(null);
    setError(null);
    setLoaded(false);
    api
      .listDriftSignals(runId)
      .then((res) => {
        if (cancelled) return;
        setDrift(res);
        setLoaded(true);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err instanceof ApiError ? err.message : String(err));
        setLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, [runId]);

  const trailing = drift ? (
    <Pill variant="neutral" size="sm">
      {drift.signals.length}
    </Pill>
  ) : undefined;

  return (
    <CollapsibleSection id="admin-drift" title="Drift" trailing={trailing}>
      {error ? (
        <EmptyState title="Failed to load drift signals" message={error} />
      ) : !loaded ? (
        <Spinner label="Loading drift" />
      ) : !drift ? (
        <p class="text-sm text-slate-500 dark:text-slate-400">
          Drift score unavailable.
        </p>
      ) : (
        <div class="space-y-3">
          <DriftGauge score={drift.score} />
          {drift.signals.length === 0 ? (
            <p class="text-xs text-slate-500 dark:text-slate-400">
              No signals recorded.
            </p>
          ) : (
            <ul class="space-y-1.5">
              {drift.signals.map((s) => (
                <li
                  key={s.id}
                  class="flex flex-wrap items-baseline gap-2 text-xs text-slate-600 dark:text-slate-300"
                >
                  <Pill variant="warning" size="sm">
                    {s.signal_kind}
                  </Pill>
                  <span class="font-mono">weight {s.weight.toFixed(2)}</span>
                  <code class="font-mono text-slate-500 dark:text-slate-400">
                    {s.producer_id}
                  </code>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </CollapsibleSection>
  );
}

export default DriftSection;
