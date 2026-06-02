/**
 * ``useGraphViewport`` — localStorage-backed FlowGraph viewport persistence.
 *
 * The W23b mandate from the user: "Graph should remember it's scale
 * and position, even after page reload." Implementation contract:
 *
 * - Key shape: ``fsm-ui:graph-viewport:${key}`` where caller-supplied
 *   ``key`` is typically ``${route}:${spec_id}``. Per-route per-spec
 *   isolation means /specs/:id and /runs/:id (RunProgressGraph)
 *   remember their own zoom for the same spec — the operator's mental
 *   model for "I last zoomed into the right side of this graph" is
 *   different per view.
 *
 * - Read once on mount, return as ``defaultViewport``. Writes are
 *   debounced (300 ms) so a continuous pan-drag doesn't thrash
 *   localStorage. Stored shape: ``{x, y, zoom}`` — matches xyflow's
 *   ``Viewport`` type. JSON-encoded.
 *
 * - SSR-safe: returns ``undefined`` for ``defaultViewport`` when
 *   ``localStorage`` is unavailable (test environment, server-render).
 *
 * - Graceful corruption recovery: a malformed JSON in storage
 *   silently falls back to ``undefined`` (xyflow re-fits via the
 *   ``fitView`` prop set in FlowGraph).
 */

import { useCallback, useMemo, useRef } from 'preact/hooks';

import type { Viewport } from '@xyflow/react';

const KEY_PREFIX = 'fsm-ui:graph-viewport:';
const PERSIST_DEBOUNCE_MS = 300;

function storageKey(key: string): string {
  return `${KEY_PREFIX}${key}`;
}

function readStoredViewport(key: string): Viewport | undefined {
  if (typeof window === 'undefined') return undefined;
  try {
    const raw = window.localStorage.getItem(storageKey(key));
    if (raw == null) return undefined;
    const parsed = JSON.parse(raw) as unknown;
    if (
      parsed != null &&
      typeof parsed === 'object' &&
      typeof (parsed as Record<string, unknown>).x === 'number' &&
      typeof (parsed as Record<string, unknown>).y === 'number' &&
      typeof (parsed as Record<string, unknown>).zoom === 'number'
    ) {
      return parsed as Viewport;
    }
  } catch {
    // Malformed JSON / quota exceeded / storage disabled — fall through.
  }
  return undefined;
}

function writeStoredViewport(key: string, viewport: Viewport): void {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(storageKey(key), JSON.stringify(viewport));
  } catch {
    // Quota exceeded / disabled — silently drop. The viewport will
    // re-default on the next page load; not worth a toast.
  }
}

export interface UseGraphViewportResult {
  /** Pass to ``<ReactFlow defaultViewport={...}>``. ``undefined`` when
   *  no value is persisted yet — caller should also keep ``fitView``
   *  set so the first paint frames the graph nicely. */
  defaultViewport: Viewport | undefined;
  /** Wire to ``<ReactFlow onMove={...}>``. Debounced internally. */
  onMove: (event: unknown, viewport: Viewport) => void;
  /** Clear the persisted viewport. Useful for a "reset view" affordance
   *  in the graph overlay; pairs with an imperative ``fitView`` call. */
  reset: () => void;
}

export function useGraphViewport(key: string): UseGraphViewportResult {
  // Read once on first render and remember the result so we don't
  // re-read on every render (storage access is cheap but predictable
  // is cheaper).
  const initial = useMemo(() => readStoredViewport(key), [key]);
  const timer = useRef<number | null>(null);

  const onMove = useCallback(
    (_event: unknown, viewport: Viewport) => {
      if (timer.current != null && typeof window !== 'undefined') {
        window.clearTimeout(timer.current);
      }
      if (typeof window !== 'undefined') {
        timer.current = window.setTimeout(() => {
          writeStoredViewport(key, viewport);
          timer.current = null;
        }, PERSIST_DEBOUNCE_MS);
      }
    },
    [key],
  );

  const reset = useCallback(() => {
    if (typeof window === 'undefined') return;
    if (timer.current != null) {
      window.clearTimeout(timer.current);
      timer.current = null;
    }
    try {
      window.localStorage.removeItem(storageKey(key));
    } catch {
      // Swallow — best-effort.
    }
  }, [key]);

  return { defaultViewport: initial, onMove, reset };
}
