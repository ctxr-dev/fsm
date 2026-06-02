/**
 * ``<RunProgressGraph>`` — full-introspection run visualisation.
 *
 * The user's explicit W22b4 mandate:
 *
 *   "If we open some specific run... graph view on that log... full
 *    introspection!!!! this is mandatory requirement."
 *
 * This component renders the FSM spec topology with the run's actual
 * traversal overlaid: visited states are colour-coded by their
 * recorded outcome (entered / exited / faulted), the current state
 * pulses for attention, and the transitions actually taken are drawn
 * solid emerald while the merely-possible transitions stay muted.
 *
 * One screen, one click — the operator goes from "/runs/X" to "I can
 * see where the run is right now, where it's been, and which leg
 * faulted" without scrolling through the event timeline.
 *
 * Data acquisition:
 *
 * - The run's spec definition (full JSON) is fetched via
 *   ``api.getSpec(spec_id)`` ONCE per run because the spec is
 *   immutable. The fetched value is cached in component state and
 *   reused as long as ``runId`` doesn't change.
 * - The overlay (per-state status + current + taken edges) is built
 *   from the manifest + state tree the parent already loaded, so we
 *   don't double-fetch.
 * - When the state tree updates (SSE pushed a new entry), the
 *   ``buildRunOverlay`` recompute lights up the next edge live; the
 *   spec definition is stable so the topology doesn't re-layout.
 */

import type { JSX } from 'preact';
import { useEffect, useMemo, useState } from 'preact/hooks';

import {
  api,
  ApiError,
  type Event as FsmEvent,
  type RunManifest,
  type SpecDetail,
  type StateNode,
} from '../lib/api';
import { specToGraph } from '../lib/specGraph';
import {
  buildRunOverlay,
  overlayProgress,
  overlayRunOnSpecGraph,
  type RunOverlay,
} from '../lib/runGraph';

import { Card } from './Card';
import { EmptyState } from './EmptyState';
import { FlowGraph } from './FlowGraph';
import { Pill } from './Pill';
import { Spinner } from './Spinner';

export interface RunProgressGraphProps {
  /** The run's manifest — needed for ``current_state``. */
  manifest: RunManifest | null;
  /** The full state tree as loaded by the route. */
  stateTree: StateNode | null;
  /** The recent event slice (used by the overlay builder as a future
   *  refinement source for accurate transition tracking). */
  events: FsmEvent[];
  /** Optional className for layout integration. */
  className?: string;
}

interface SpecCache {
  spec_id: string;
  detail: SpecDetail;
}

function readSpecId(manifest: RunManifest | null): string | null {
  return manifest?.fsm_spec_id ?? null;
}

export function RunProgressGraph({
  manifest,
  stateTree,
  events,
  className = '',
}: RunProgressGraphProps): JSX.Element {
  const [spec, setSpec] = useState<SpecCache | null>(null);
  const [error, setError] = useState<string | null>(null);

  const specId = readSpecId(manifest);

  // Spec fetch — once per (run, spec_id) tuple. The spec is immutable
  // post-registration so we never need to re-fetch unless the run
  // changes underneath us.
  useEffect(() => {
    if (specId === null) return;
    if (spec?.spec_id === specId) return;
    let cancelled = false;
    setError(null);
    api
      .getSpec(specId)
      .then((detail) => {
        if (cancelled) return;
        setSpec({ spec_id: specId, detail });
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err instanceof ApiError ? err.message : String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [specId, spec?.spec_id]);

  const baseGraph = useMemo(() => {
    if (!spec) return null;
    return specToGraph(spec.detail.definition);
  }, [spec]);

  const overlay = useMemo<RunOverlay>(
    () => buildRunOverlay(manifest, stateTree, events),
    [manifest, stateTree, events],
  );

  const overlaid = useMemo(() => {
    if (!baseGraph) return null;
    return overlayRunOnSpecGraph(baseGraph, overlay);
  }, [baseGraph, overlay]);

  const progress = useMemo(
    () => overlayProgress(overlay, baseGraph?.nodes.length ?? 0),
    [overlay, baseGraph],
  );

  if (error) {
    return (
      <Card className={className} title="Progress graph">
        <EmptyState title="Spec unavailable" message={error} />
      </Card>
    );
  }
  if (!spec || !baseGraph || !overlaid) {
    return (
      <Card className={className} title="Progress graph">
        <div class="flex items-center justify-center py-12">
          <Spinner label="Loading spec topology" />
        </div>
      </Card>
    );
  }
  if (baseGraph.nodes.length === 0) {
    return (
      <Card className={className} title="Progress graph">
        <EmptyState
          title="No states in this spec"
          message="The registered spec declares no states; nothing to render."
        />
      </Card>
    );
  }

  return (
    <Card className={className} title="Progress graph">
      <header class="flex flex-wrap items-center gap-3 px-3 pt-2 pb-3 text-xs">
        <span class="text-slate-700 dark:text-slate-300">
          <strong class="font-semibold">{progress.visited}</strong>
          <span class="text-slate-500 dark:text-slate-400"> / {progress.total}</span>
          {' '}states visited
        </span>
        <div class="flex items-center gap-1.5">
          <Pill variant="success" size="sm">
            <span aria-hidden="true" class="inline-block h-1.5 w-1.5 rounded-full bg-emerald-500 mr-1" />
            exited
          </Pill>
          <Pill variant="warning" size="sm">
            <span aria-hidden="true" class="inline-block h-1.5 w-1.5 rounded-full bg-amber-500 mr-1" />
            current
          </Pill>
          <Pill variant="danger" size="sm">
            <span aria-hidden="true" class="inline-block h-1.5 w-1.5 rounded-full bg-red-500 mr-1" />
            faulted
          </Pill>
          <Pill variant="neutral" size="sm">
            <span aria-hidden="true" class="inline-block h-1.5 w-1.5 rounded-full bg-slate-400 mr-1" />
            not visited
          </Pill>
        </div>
        {overlay.faultedStateId ? (
          <span class="text-red-700 dark:text-red-300 font-mono">
            fault at <code>{overlay.faultedStateId}</code>
          </span>
        ) : null}
        {overlay.currentStateId ? (
          <span class="ml-auto text-slate-600 dark:text-slate-400">
            current state: <code class="font-mono text-slate-900 dark:text-slate-100">{overlay.currentStateId}</code>
          </span>
        ) : null}
      </header>
      <div class="px-1 pb-1" style={{ height: '420px' }}>
        <FlowGraph
          nodes={overlaid.nodes}
          edges={overlaid.edges}
          viewportKey={specId ? `runs.progress:${specId}` : undefined}
        />
      </div>
    </Card>
  );
}

export default RunProgressGraph;
