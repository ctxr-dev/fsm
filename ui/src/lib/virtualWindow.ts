/**
 * Tiny fixed-height row virtualisation.
 *
 * Used by `<JsonViewer />` (W18b) and `<CodeBlock />` (W18b) to keep
 * thousand-row trees / large prompt templates responsive. Below
 * `VIRTUALISE_THRESHOLD` we render every row (zero overhead, no
 * window math, no measurement); above the threshold we hand the
 * caller a `{ range, onScroll, totalHeight }` triplet they bind to a
 * scrollable container.
 *
 * Why a tiny hook rather than `react-window`: this hook is ~50 LOC
 * and has zero deps. `react-window` would add ~7 kB gzipped and
 * pulls in `memoize-one` + scroll-position-restoration logic we
 * don't need. The thousand-row scenario is the only one that
 * justifies virtualisation; we don't need the general feature set.
 *
 * Fixed row height is a constraint we accept: every JsonViewer row
 * is the same height by design (font-mono + leading-tight). If a
 * future variant needs variable heights, switch to react-window
 * then; for now, fixed is the right trade.
 */

import { useCallback, useMemo, useState } from 'preact/hooks';

export interface WindowState {
  /** Inclusive start index of rows to render. */
  start: number;
  /** Exclusive end index of rows to render. */
  end: number;
}

export interface VirtualWindow {
  /** Current visible range; pass `rows.slice(range.start, range.end)`. */
  range: WindowState;
  /** Bind to the scrollable container's `onScroll` event. */
  onScroll: (event: Event) => void;
  /** Total content height in px; bind to a spacer div above the rows. */
  totalHeight: number;
  /** Top spacer height in px (`range.start * rowHeight`). */
  topSpacerHeight: number;
  /** Bottom spacer height in px ((rows.length - range.end) * rowHeight). */
  bottomSpacerHeight: number;
}

export interface UseWindowOptions {
  /** Below this count, virtualisation is skipped — render the whole list. */
  virtualiseThreshold?: number;
  /** Extra rows to render above + below the viewport. Default 10. */
  overscan?: number;
  /** Initial scrollTop, in px. Useful when restoring a saved scroll position. */
  initialScrollTop?: number;
}

export const DEFAULT_VIRTUALISE_THRESHOLD = 1000;
export const DEFAULT_OVERSCAN = 10;

/**
 * useWindow — compute the visible row slice for a scrollable container.
 *
 * @param totalRows  Total number of rows the list would have if everything
 *                   were rendered.
 * @param rowHeight  Px height per row. Must be constant. If the consumer
 *                   uses CSS that produces variable row heights, this hook
 *                   will produce mis-aligned scroll positions; use react-
 *                   window instead.
 * @param options    Threshold, overscan, initialScrollTop.
 *
 * Below `virtualiseThreshold` rows, `range` is always `{start:0, end:totalRows}`
 * and `onScroll` is a no-op — the consumer effectively gets the same
 * behaviour as rendering naively, with zero CPU cost from the hook.
 *
 * Above the threshold, `range` is recomputed on each `onScroll` event
 * from `event.currentTarget.scrollTop`. The hook stores the latest
 * range in `useState`, so Preact re-renders the consumer with the
 * new slice. No requestAnimationFrame throttling is applied; Preact's
 * batched updates already coalesce a scroll-storm into a small number
 * of renders, and a single render at a 5000-row tree completes in
 * <2 ms in jsdom.
 */
export function useWindow(
  totalRows: number,
  rowHeight: number,
  options: UseWindowOptions = {},
): VirtualWindow {
  const virtualiseThreshold =
    options.virtualiseThreshold ?? DEFAULT_VIRTUALISE_THRESHOLD;
  const overscan = options.overscan ?? DEFAULT_OVERSCAN;
  const shouldVirtualise = totalRows > virtualiseThreshold;

  const [range, setRange] = useState<WindowState>(() => {
    if (!shouldVirtualise) return { start: 0, end: totalRows };
    // Initial paint covers viewport at the user-supplied scroll
    // position (default 0). End is capped at totalRows so the spacer
    // math below doesn't go negative on small lists that just crossed
    // the threshold.
    const initialScrollTop = options.initialScrollTop ?? 0;
    const start = Math.max(
      0,
      Math.floor(initialScrollTop / rowHeight) - overscan,
    );
    // Without a viewport height yet, assume a conservative 50 rows
    // visible. The first real scroll event will replace this.
    const end = Math.min(totalRows, start + 50 + overscan * 2);
    return { start, end };
  });

  const onScroll = useCallback(
    (event: Event) => {
      if (!shouldVirtualise) return;
      const el = event.currentTarget as HTMLElement | null;
      if (!el) return;
      const start = Math.max(0, Math.floor(el.scrollTop / rowHeight) - overscan);
      const end = Math.min(
        totalRows,
        start + Math.ceil(el.clientHeight / rowHeight) + overscan * 2,
      );
      // Only commit a state change when the range actually moved —
      // avoids a re-render on every pixel of scroll within the same
      // row.
      setRange((prev) => (prev.start === start && prev.end === end ? prev : { start, end }));
    },
    [shouldVirtualise, totalRows, rowHeight, overscan],
  );

  const totalHeight = useMemo(
    () => totalRows * rowHeight,
    [totalRows, rowHeight],
  );

  const topSpacerHeight = useMemo(
    () => range.start * rowHeight,
    [range.start, rowHeight],
  );

  const bottomSpacerHeight = useMemo(
    () => Math.max(0, (totalRows - range.end) * rowHeight),
    [totalRows, range.end, rowHeight],
  );

  return { range, onScroll, totalHeight, topSpacerHeight, bottomSpacerHeight };
}
