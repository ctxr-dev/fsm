/**
 * FlowGraph — wraps @xyflow/react for FSM-state visualisations.
 *
 * Used by Specs v2 (W18e) to render the spec's state graph, and by
 * Topology (W18f) to render producers ↔ consumers. Wraps the library
 * to:
 *
 *   - Apply a dagre auto-layout when callers pass nodes without
 *     coordinates (the most common path: we know the graph topology
 *     but not the pixel positions).
 *   - Style nodes with theme tokens (light/dark) so colour stays
 *     consistent with the rest of the dashboard.
 *   - Expose three node-style variants: state, worker, terminal —
 *     each with a distinctive border colour matching the existing
 *     status-pill palette.
 *
 * Edge cases:
 *   - Empty graph (0 nodes) renders an EmptyState fallback.
 *   - Cyclic graphs are dagre's strength; we render them faithfully.
 *
 * Tests under __tests__/FlowGraph.test.tsx exercise: empty graph,
 * dagre layout produces non-overlapping x/y, custom node click fires
 * onNodeClick.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'preact/hooks';
import type { JSX } from 'preact';
import { createContext } from 'preact';
import dagre from 'dagre';
import {
  BackgroundVariant,
  PanOnScrollMode,
  ReactFlow,
  Background,
  Controls,
  Handle,
  MarkerType,
  MiniMap,
  Position,
  useReactFlow,
  useStore,
  type Edge,
  type Node,
  type NodeProps,
  type ReactFlowState,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';

import { useGraphViewport } from '../lib/graphViewport';
import { Tooltip } from './Tooltip';
import { FsmEdge, FsmEdgeClickContext } from './FsmEdge';
import { Spinner } from './Spinner';
import {
  applyElkLayout,
  type ElkEdgeRouting,
  type LayoutResult,
} from '../lib/elkLayout';

export type FlowNodeKind = 'state' | 'worker' | 'terminal' | 'producer' | 'consumer' | 'inline' | 'loop';

/**
 * Per-iteration chip payload. Mirrors ``LoopIterationEntry`` in
 * ``runGraph.ts`` but is duplicated here so FlowGraph stays free of a
 * direct import from the run-overlay layer (FlowGraph is reused by
 * Topology, which has no notion of run iterations).
 */
export interface FlowLoopIteration {
  entry_id: string;
  iteration_n: number | null;
  status: string;
}

export interface FlowNodeData extends Record<string, unknown> {
  kind: FlowNodeKind;
  /** Visible (truncated) label rendered inside the node. */
  label: string;
  /** Visible (truncated) sublabel under the main label. */
  sublabel?: string;
  /** Full untruncated label surfaced via hover tooltip + click-to-Sheet.
   *  Defaults to `label` when omitted. */
  fullLabel?: string;
  /** Full untruncated sublabel surfaced via hover tooltip.
   *  Defaults to `sublabel` when omitted. */
  fullSublabel?: string;
  /** Original spec state object (typed loosely so FlowGraph stays
   *  domain-agnostic for Topology / Drift consumers). Surfaced to
   *  onNodeClick so the caller can render an inspector Sheet. */
  state?: Record<string, unknown>;
  /** PR 5: loop-state discriminator. When true, FlowGraph renders the
   *  loop variant (custom node type) with an iteration chip strip
   *  instead of the bare FsmNode card. */
  isLoop?: boolean;
  /** PR 5: spec-declared iteration ceiling. Used by the LoopNode
   *  renderer to colour empty chip slots until the run actually fills
   *  them. */
  loopMaxIterations?: number;
  /** PR 5: number of state-entry rows the run actually produced for
   *  this loop state. Drives the "×N" badge in the loop node header. */
  iterationCount?: number;
  /** PR 5: chronological list of per-iteration entries. Each chip in
   *  the loop node's strip is sourced from one element. */
  iterationEntries?: FlowLoopIteration[];
  /**
   * Viewport-zoom-aware render mode. When `'compact'`, the node renders a
   * small card (~100×44) with just the truncated state name; when `'full'`
   * (or undefined — back-compat default), the existing 160×60 card with
   * the kind chip + full label is rendered. FlowGraph stamps this onto
   * every node's data whenever the active viewport zoom crosses
   * {@link DETAIL_LEVEL_ZOOM_THRESHOLD}.
   */
  detailLevel?: DetailLevel;
}

/** Zoom threshold below which (≤) the graph switches to compact cards.
 *  Exported so tests + the LoopNode can share the same number.
 *
 *  Lowered back to 0.4 to pair with the React Flow ``minZoom={0.45}``
 *  floor introduced alongside the vertical-orientation fix: at the new
 *  minimum zoom the graph never settles in compact mode, so labels +
 *  full-detail cards stay readable at the equilibrium fit-view zoom.
 *  Compact mode now only kicks in for genuinely tiny zooms (the user
 *  has to Cmd/Ctrl-wheel BELOW the floor, which means they've
 *  explicitly requested the bird's-eye view). */
export const DETAIL_LEVEL_ZOOM_THRESHOLD = 0.4;

/** Maximum visible characters for a state-name label in compact mode.
 *  Anything longer is truncated to this prefix plus an ellipsis. The
 *  full label remains available via tooltip + the inspector Sheet. */
export const COMPACT_LABEL_MAX_CHARS = 14;

export type DetailLevel = 'full' | 'compact';

/** Truncate a label for compact-mode rendering. Returns the original
 *  string when it already fits inside {@link COMPACT_LABEL_MAX_CHARS}. */
export function truncateCompactLabel(label: string): string {
  if (typeof label !== 'string') return '';
  if (label.length <= COMPACT_LABEL_MAX_CHARS) return label;
  return `${label.slice(0, COMPACT_LABEL_MAX_CHARS)}…`;
}

export interface FlowGraphProps {
  nodes: readonly Node<FlowNodeData>[];
  edges: readonly Edge[];
  /** PR 5: click handler for an iteration chip inside a loop node.
   *  Receives the entry_id stamped on the chip; the run-detail route
   *  wires this to ``openStateEntrySheet`` so a click on a specific
   *  iteration opens the per-iteration inspector (Tab 1 = run values
   *  for that entry; Tab 3 = events filtered to that entry_id). */
  onIterationClick?: (entryId: string) => void;
  /** When true (default), runs a dagre layered layout that OVERWRITES any
   * per-node position. Pass `false` if the caller has already assigned
   * positions (e.g. a saved manual layout) and wants them preserved. */
  autoLayout?: boolean;
  /** Layout direction. Pinned to 'TB' (top to bottom) — vertical
   *  orientation is a product-level invariant for both the spec graph
   *  and the run graph. The prop is preserved for back-compat callers
   *  but its value is ignored: every render lays out top-to-bottom and
   *  stamps Top/Bottom handle anchors. Future re-introduction of
   *  horizontal layout would resurrect this enum. */
  direction?: 'LR' | 'TB';
  /** Click handler on a node (e.g. open the state-details Sheet). */
  onNodeClick?: (id: string, data: FlowNodeData) => void;
  /** Click handler on an edge. The second arg carries the edge's
   *  data payload (set by decorateEdges); typed loosely so callers can
   *  cast to FsmEdgeData or their own shape. Backward-compatible:
   *  existing handlers that take only `id` keep working. */
  onEdgeClick?: (id: string, data?: Record<string, unknown>) => void;
  /** Selected node id; renders with an emphasis ring. */
  selectedNodeId?: string;
  /** Show the mini-map control. Default true for >10 nodes, false otherwise. */
  miniMap?: boolean;
  /** Show pan/zoom controls. Default true. */
  controls?: boolean;
  /** Show background dots. Default true. */
  background?: boolean;
  /** Outer container Tailwind class. */
  className?: string;
  /**
   * W23b: opt-in viewport persistence key. When set, the FlowGraph
   * reads its initial pan + zoom from
   * ``localStorage[fsm-ui:graph-viewport:${viewportKey}]`` and writes
   * it back (debounced 300ms) on every pan / zoom interaction.
   * Recommended key shape: ``${route}:${spec_id}`` so /specs/:id and
   * /runs/:id (RunProgressGraph) remember their own zoom independently
   * even when looking at the same spec.
   *
   * Omitted → falls back to the pre-W23b ``fitView`` behaviour on
   * every remount.
   */
  viewportKey?: string;
}

// Clean-slate rebuild: every node uses the SAME card dimensions
// (worker, inline, loop, terminal). The loop node carries a small ×N
// badge but stays the same size as its siblings so a graph with many
// loops doesn't have one node 30% of the canvas width. Detail surfaces
// (iteration chip strip, per-iteration entries) live in the
// StateEntrySheetBody, not on the graph card.
// Card dimensions tuned to fit an 18-node FSM in a 1270×733 graph
// viewport at sensible zoom without an unreadable shrink:
//   LR layout: 14 layers × 140 (node + spacing) ≈ 2000 px wide;
//              fit-view zoom ≈ 0.6 → nodes render ~85 px wide.
//   TB layout: 14 layers × 100 (node + spacing) ≈ 1400 px tall;
//              fit-view zoom ≈ 0.45 → nodes render ~63 px wide.
// Card content is the kind chip + label, both still legible at
// 130×56 since the font sizes stay 10-12 px.
const NODE_WIDTH = 160;
const NODE_HEIGHT = 60;

/**
 * Compact-mode card dimensions. Roughly 60 % of the full size so a
 * 14-layer chain (e.g. skill-code-review v4) fits a 16:9 viewport at
 * fit-view zoom 0.55-0.7 instead of the ~0.36 the full layout
 * collapses to. The chip is hidden and the label is truncated so a
 * smaller box stays legible at that zoom.
 */
const NODE_WIDTH_COMPACT = 100;
const NODE_HEIGHT_COMPACT = 44;

/** Per-detail-level node dimensions. Used by both the dagre and ELK
 *  layout branches so the two engines reserve the same slack as the
 *  card they render at the active detail level. */
const NODE_DIMS_BY_DETAIL: Record<DetailLevel, { width: number; height: number }> = {
  full: { width: NODE_WIDTH, height: NODE_HEIGHT },
  compact: { width: NODE_WIDTH_COMPACT, height: NODE_HEIGHT_COMPACT },
};

const NODE_KIND_CLASSES: Record<FlowNodeKind, string> = {
  state:
    'border-emerald-500 bg-emerald-50 dark:bg-emerald-900/30 text-emerald-900 dark:text-emerald-100',
  worker:
    'border-sky-500 bg-sky-50 dark:bg-sky-900/30 text-sky-900 dark:text-sky-100',
  inline:
    'border-violet-500 bg-violet-50 dark:bg-violet-900/30 text-violet-900 dark:text-violet-100',
  // PR 5: loop nodes get their own palette so the loop variant reads
  // as a structurally-different shape on the graph (orange/sky blend
  // — visually adjacent to worker since loops contain worker bodies,
  // distinct enough to spot at a glance on a dense run topology).
  loop:
    'border-indigo-500 bg-indigo-50 dark:bg-indigo-900/30 text-indigo-900 dark:text-indigo-100',
  terminal:
    'border-slate-500 bg-slate-100 dark:bg-slate-900/50 text-slate-700 dark:text-slate-200',
  producer:
    'border-amber-500 bg-amber-50 dark:bg-amber-900/30 text-amber-900 dark:text-amber-100',
  consumer:
    'border-cyan-500 bg-cyan-50 dark:bg-cyan-900/30 text-cyan-900 dark:text-cyan-100',
};

/** Exported for FsmNode.test.tsx so the compact / full render paths
 *  can be exercised directly without round-tripping through the full
 *  FlowGraph orchestrator. Not part of the public surface — callers
 *  outside this module should keep using FlowGraph + node.data. */
export function FsmNode({ data, selected, sourcePosition, targetPosition }: NodeProps<Node<FlowNodeData>>): JSX.Element {
  const kind = data.kind;
  // Handles are the connection anchors xyflow draws edges into.
  // Without these, no edge has anywhere to land and the connection
  // lines render zero-length / invisible. Position defaults match
  // the dagre layout's direction (LR: source=right, target=left).
  const sp = sourcePosition ?? Position.Right;
  const tp = targetPosition ?? Position.Left;
  // The pixel dimensions are constants matched against the dagre
  // layout numbers; staying inline keeps the two values in literal
  // sync. Suppress the no-inline-styles lint for this single case.
  /* eslint-disable-next-line react/forbid-dom-props -- xyflow nodes need fixed pixel dimensions matched to dagre layout */
  // W23b regression fix: the node card is INTENTIONALLY content-thin.
  // The only on-card affordances are the tiny uppercase kind chip and
  // the bold state-id label. Any additional structural / canonical
  // information (worker role, inline handler, output counts, transition
  // counts, post-validations, verifier presence) is reachable via the
  // click-to-open inspector Sheet, which is the source of truth for
  // per-state detail. Rendering a sublabel here re-introduces the W22
  // chaff the user explicitly rejected ("7 outputs / post-val x2" /
  // "worker: tree-descender" beneath every node).
  //
  // Layout:
  //   +----------------------------------------------+
  //   | KIND                                         |  row 1: kind chip (tiny)
  //   | synthesize_release_readiness                 |  row 2: label, FULL width
  //   +----------------------------------------------+
  // Click the card to open the full state inspector.
  const runStatus = typeof data.runStatus === 'string' ? data.runStatus : undefined;
  const isCurrent = data.isCurrent === true;
  // RUN-STATUS palette takes precedence over the kind palette when a
  // node carries a runStatus (set by overlayRunOnSpecGraph). This makes
  // the status the dominant visual on the inner card so the legend's
  // promise (amber=current, emerald=visited, red=fault, slate=pending)
  // actually shows up on the graph. The kind chip remains visible at
  // the top of the card so the operator still sees worker/inline/etc.
  const STATUS_CLASSES: Record<string, string> = {
    not_visited:
      'border-slate-400 bg-slate-100 dark:bg-slate-800/60 text-slate-500 dark:text-slate-400 opacity-70',
    entered:
      'border-amber-500 bg-amber-50 dark:bg-amber-900/40 text-amber-900 dark:text-amber-100',
    exited:
      'border-emerald-500 bg-emerald-50 dark:bg-emerald-900/40 text-emerald-900 dark:text-emerald-100',
    faulted:
      'border-red-500 bg-red-50 dark:bg-red-900/40 text-red-900 dark:text-red-100',
  };
  const paletteClass = runStatus
    ? STATUS_CLASSES[runStatus] ?? NODE_KIND_CLASSES[kind]
    : NODE_KIND_CLASSES[kind];
  const currentRing = isCurrent
    ? 'ring-2 ring-amber-400 ring-offset-2 ring-offset-white dark:ring-offset-slate-900'
    : '';
  // W22b PR 4: the run graph's current node gets a continuous amber
  // ring pulse on top of the static ring above so the operator's eye
  // lands on the active state at a glance even on a busy graph. The
  // `fsm-pulse-current` class is owned by theme.css and respects
  // prefers-reduced-motion (falls back to a static amber outline).
  const currentPulse = isCurrent ? 'fsm-pulse-current' : '';
  // W23d 1-hop hover highlight. FlowGraph stamps three transient flags
  // onto node.data while a hover is active:
  //   data.isHovered    — this is the node the cursor is on (or one
  //                       endpoint of the hovered edge)
  //   data.highlighted  — this node is a neighbour (1 hop away) of the
  //                       hovered node, or it is the source/target of
  //                       the hovered edge
  //   data.dimmed       — something else is hovered and this node is
  //                       NOT in the highlighted set
  // When nothing is hovered all three flags are false and the node
  // renders unchanged.
  const isHovered = data.isHovered === true;
  const isHighlighted = data.highlighted === true;
  const isDimmed = data.dimmed === true;
  const hoverRing = isHovered
    ? 'ring-2 ring-amber-400 ring-offset-2 ring-offset-white dark:ring-offset-slate-900'
    : isHighlighted
    ? 'ring-1 ring-amber-300 ring-offset-1 ring-offset-white dark:ring-offset-slate-900'
    : '';
  const dimClass = isDimmed ? 'opacity-30' : '';
  // W23e: viewport-zoom-aware detail toggle. When the live zoom drops at
  // or below DETAIL_LEVEL_ZOOM_THRESHOLD, FlowGraph stamps
  // data.detailLevel='compact' on every node; we render a denser card
  // here so a 14-layer chain stays legible at fit-view zoom without an
  // unreadable text shrink. The full label remains accessible via the
  // hover tooltip + the inspector Sheet.
  const detailLevel: DetailLevel = data.detailLevel === 'compact' ? 'compact' : 'full';
  const isCompact = detailLevel === 'compact';
  const dims = NODE_DIMS_BY_DETAIL[detailLevel];
  const labelPrefix = typeof data.labelPrefix === 'string' ? data.labelPrefix : '';
  const visibleLabel = isCompact ? truncateCompactLabel(data.label) : data.label;
  return (
    <div
      class={[
        'fsm-node relative rounded-md border-2 shadow-sm',
        isCompact ? 'fsm-node-compact px-2 py-1' : 'px-4 py-3',
        // justify-center centers the kind/label block vertically within
        // the (fixed) node height. gap-1 gives the two rows even
        // breathing room.
        isCompact
          ? 'flex items-center justify-center overflow-hidden'
          : 'flex flex-col gap-1 justify-center overflow-hidden',
        // Smooth fade between dim / un-dim states. motion-reduce
        // disables the transition for users with
        // prefers-reduced-motion (existing pattern across the dashboard).
        'transition-opacity duration-150 motion-reduce:transition-none',
        paletteClass,
        selected ? 'ring-2 ring-emerald-400 ring-offset-1' : '',
        currentRing,
        currentPulse,
        hoverRing,
        dimClass,
      ].join(' ')}
      data-detail-level={detailLevel}
      /* eslint-disable-next-line react/forbid-dom-props -- xyflow nodes need fixed pixel dimensions matched to dagre layout */
      style={{ width: `${dims.width}px`, minHeight: `${dims.height}px` }}
    >
      <Handle
        type="target"
        position={tp}
        style={{ background: 'currentColor', width: 8, height: 8, border: 'none' }}
      />
      {isCompact ? (
        // Compact card: no kind chip, no two-row stack. A single bigger
        // label fills the small box so the text stays readable at low
        // zoom. Truncated to COMPACT_LABEL_MAX_CHARS + ellipsis; full
        // label surfaces via the existing Tooltip wrap below.
        <Tooltip content={data.fullLabel ?? data.label} delay={400}>
          <span
            class="font-semibold text-[14px] truncate block w-full text-center leading-tight"
            data-testid="fsm-node-label-compact"
          >
            {labelPrefix}
            {visibleLabel}
          </span>
        </Tooltip>
      ) : (
        <>
          <span
            class="text-[9px] uppercase tracking-wider opacity-60 leading-none"
            data-testid="fsm-node-kind-chip"
          >
            {kind}
          </span>
          <Tooltip content={data.fullLabel ?? data.label} delay={400}>
            <span class="font-semibold text-sm truncate block w-full leading-tight">
              {labelPrefix}
              {data.label}
            </span>
          </Tooltip>
        </>
      )}
      <Handle
        type="source"
        position={sp}
        style={{ background: 'currentColor', width: 8, height: 8, border: 'none' }}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Loop node — uniform card same shape/size as FsmNode, plus a ×N badge.
// ---------------------------------------------------------------------------
//
// Clean-slate rebuild rationale: the previous loop node embedded a
// per-iteration chip strip directly on the graph, which made the loop
// card 30 %+ of the canvas width and forced every other node to flow
// around it. That broke the layout the user wanted (uniform card grid).
// Per-iteration affordances now live in the inspector Sheet
// (StateEntrySheetBody → "Iterations" section); the graph card just
// carries a small "×N" badge in the top-right so the operator can see
// at a glance how many times the loop ran.

/**
 * Per-status palette for the iteration chip rendered inside the
 * StateEntrySheetBody's "Iterations" section. Exported so the sheet
 * (which owns the chip strip now) renders the same status colours as
 * the rest of the run graph.
 */
export const LOOP_CHIP_STATUS_CLASSES: Record<string, string> = {
  faulted:
    'border-red-500 bg-red-100 dark:bg-red-900/50 text-red-900 dark:text-red-100',
  entered:
    'border-amber-500 bg-amber-100 dark:bg-amber-900/50 text-amber-900 dark:text-amber-100',
  exited:
    'border-emerald-500 bg-emerald-100 dark:bg-emerald-900/50 text-emerald-900 dark:text-emerald-100',
  completed:
    'border-emerald-500 bg-emerald-100 dark:bg-emerald-900/50 text-emerald-900 dark:text-emerald-100',
};

/** Helper: pick the chip class for one iteration's status. */
export function loopChipClass(status: string): string {
  return (
    LOOP_CHIP_STATUS_CLASSES[status.toLowerCase()] ??
    'border-slate-400 bg-slate-100 dark:bg-slate-800/60 text-slate-700 dark:text-slate-300'
  );
}

/** Context bridge for iteration-chip click dispatch from any
 *  descendant of FlowGraph. Kept exported because the route still
 *  passes ``onIterationClick`` and other future surfaces (the
 *  inspector Sheet) can subscribe to it without prop-drilling. */
export const LoopIterationClickContext = createContext<
  ((entryId: string) => void) | null
>(null);

/** Exported for FsmNode.test.tsx — same rationale as `FsmNode`. */
export function LoopNode({
  data,
  selected,
  sourcePosition,
  targetPosition,
}: NodeProps<Node<FlowNodeData>>): JSX.Element {
  const sp = sourcePosition ?? Position.Bottom;
  const tp = targetPosition ?? Position.Top;

  const iterations = Array.isArray(data.iterationEntries)
    ? data.iterationEntries
    : [];
  const iterationCount =
    typeof data.iterationCount === 'number'
      ? data.iterationCount
      : iterations.length;
  const maxIterations =
    typeof data.loopMaxIterations === 'number' ? data.loopMaxIterations : 0;

  const runStatus = typeof data.runStatus === 'string' ? data.runStatus : undefined;
  const isCurrent = data.isCurrent === true;

  // Status palette mirrors FsmNode so a loop card lights up the same
  // emerald/amber/red as worker siblings in the same run state.
  const STATUS_CLASSES: Record<string, string> = {
    not_visited:
      'border-slate-400 bg-slate-100 dark:bg-slate-800/60 text-slate-500 dark:text-slate-400 opacity-70',
    entered:
      'border-amber-500 bg-amber-50 dark:bg-amber-900/40 text-amber-900 dark:text-amber-100',
    exited:
      'border-emerald-500 bg-emerald-50 dark:bg-emerald-900/40 text-emerald-900 dark:text-emerald-100',
    faulted:
      'border-red-500 bg-red-50 dark:bg-red-900/40 text-red-900 dark:text-red-100',
  };
  const paletteClass = runStatus
    ? STATUS_CLASSES[runStatus] ?? NODE_KIND_CLASSES.loop
    : NODE_KIND_CLASSES.loop;

  const currentRing = isCurrent
    ? 'ring-2 ring-amber-400 ring-offset-2 ring-offset-white dark:ring-offset-slate-900'
    : '';
  const currentPulse = isCurrent ? 'fsm-pulse-current' : '';

  // Hover-highlight flags — same contract as FsmNode.
  const isHovered = data.isHovered === true;
  const isHighlighted = data.highlighted === true;
  const isDimmed = data.dimmed === true;
  const hoverRing = isHovered
    ? 'ring-2 ring-amber-400 ring-offset-2 ring-offset-white dark:ring-offset-slate-900'
    : isHighlighted
    ? 'ring-1 ring-amber-300 ring-offset-1 ring-offset-white dark:ring-offset-slate-900'
    : '';
  const dimClass = isDimmed ? 'opacity-30' : '';

  // The badge is suppressed when the spec hasn't declared a loop body
  // AND no run iterations have landed yet — otherwise we'd show a
  // misleading "×0" on a static spec graph.
  const showBadge = iterationCount > 0 || maxIterations > 0;
  const badgeText =
    iterationCount > 0
      ? `×${iterationCount}${maxIterations > 0 ? ` / ${maxIterations}` : ''}`
      : `×0 / ${maxIterations}`;
  const badgeTitle =
    maxIterations > 0
      ? `${iterationCount} of ${maxIterations} iterations`
      : `${iterationCount} iterations`;

  // W23e: same detail-toggle contract as FsmNode. The ×N badge stays
  // visible in compact mode at a smaller font so the operator still sees
  // iteration counts at low zoom; the kind chip is dropped to free
  // vertical space.
  const detailLevel: DetailLevel = data.detailLevel === 'compact' ? 'compact' : 'full';
  const isCompact = detailLevel === 'compact';
  const dims = NODE_DIMS_BY_DETAIL[detailLevel];
  const labelPrefix = typeof data.labelPrefix === 'string' ? data.labelPrefix : '';
  const visibleLabel = isCompact ? truncateCompactLabel(data.label) : data.label;

  return (
    <div
      class={[
        'fsm-node fsm-loop-node relative rounded-md border-2 shadow-sm',
        isCompact ? 'fsm-loop-node-compact px-2 py-1' : 'px-4 py-3',
        isCompact
          ? 'flex items-center justify-center overflow-hidden'
          : 'flex flex-col gap-1 justify-center overflow-hidden',
        'transition-opacity duration-150 motion-reduce:transition-none',
        paletteClass,
        selected ? 'ring-2 ring-emerald-400 ring-offset-1' : '',
        currentRing,
        currentPulse,
        hoverRing,
        dimClass,
      ].join(' ')}
      data-testid="loop-node"
      data-detail-level={detailLevel}
      /* eslint-disable-next-line react/forbid-dom-props -- xyflow nodes need fixed pixel dimensions matched to dagre layout */
      style={{ width: `${dims.width}px`, minHeight: `${dims.height}px` }}
    >
      <Handle
        type="target"
        position={tp}
        style={{ background: 'currentColor', width: 8, height: 8, border: 'none' }}
      />
      {/* ×N badge floats in the top-right corner so it doesn't push the
          kind/label rows around. position:absolute keeps it out of the
          flex layout — the card visual matches FsmNode pixel-for-pixel
          aside from this single decoration. In compact mode the badge
          font shrinks so it doesn't dominate the smaller card. */}
      {showBadge ? (
        <span
          class={[
            'absolute font-mono leading-none rounded bg-black/10 dark:bg-white/10',
            isCompact ? 'top-0 right-0 text-[8px] px-0.5' : 'top-1 right-1 text-[9px] px-1 py-0.5',
          ].join(' ')}
          title={badgeTitle}
          data-testid="loop-iteration-badge"
        >
          {badgeText}
        </span>
      ) : null}
      {isCompact ? (
        <Tooltip content={data.fullLabel ?? data.label} delay={400}>
          <span
            class="font-semibold text-[14px] truncate block w-full text-center leading-tight"
            data-testid="loop-node-label-compact"
          >
            {labelPrefix}
            {visibleLabel}
          </span>
        </Tooltip>
      ) : (
        <>
          <span
            class="text-[9px] uppercase tracking-wider opacity-60 leading-none"
            data-testid="loop-node-kind-chip"
          >
            loop
          </span>
          <Tooltip content={data.fullLabel ?? data.label} delay={400}>
            <span class="font-semibold text-sm truncate block w-full leading-tight">
              {labelPrefix}
              {data.label}
            </span>
          </Tooltip>
        </>
      )}
      <Handle
        type="source"
        position={sp}
        style={{ background: 'currentColor', width: 8, height: 8, border: 'none' }}
      />
    </div>
  );
}

const NODE_TYPES = { fsmNode: FsmNode, loopNode: LoopNode };
const EDGE_TYPES = { fsmEdge: FsmEdge };

/**
 * Tiny child of <ReactFlow> that subscribes to the live viewport zoom
 * via React Flow's internal store and lifts a `detailLevel` ('full'
 * vs 'compact') up to FlowGraph via callback. Lives INSIDE the
 * ReactFlow context (which is the only place `useStore` is allowed)
 * but renders nothing — the actual card / layout swap happens up in
 * the orchestrator.
 *
 * The threshold is exclusive: zoom > DETAIL_LEVEL_ZOOM_THRESHOLD →
 * full, zoom ≤ threshold → compact. The check runs in a useEffect so
 * the parent only re-renders when the level actually flips (not on
 * every transform change while the user pans).
 */
function ZoomDetailWatcher({
  onChange,
}: {
  onChange: (level: DetailLevel) => void;
}): JSX.Element | null {
  // transform = [x, y, zoom]; index 2 is the live zoom level.
  const zoom = useStore((s: ReactFlowState) => s.transform[2]);
  useEffect(() => {
    const next: DetailLevel = zoom > DETAIL_LEVEL_ZOOM_THRESHOLD ? 'full' : 'compact';
    onChange(next);
  }, [zoom, onChange]);
  return null;
}

/**
 * Re-runs ``fitView`` whenever the detail level flips. The initial
 * ``fitView`` on mount frames the FULL layout; once
 * ``ZoomDetailWatcher`` flips us to compact (because the full-mode
 * fit-zoom was <= DETAIL_LEVEL_ZOOM_THRESHOLD), the layout pass produces
 * a much smaller bbox and we want the viewport to re-frame it so the
 * compact card text is actually readable.
 *
 * Only refits when the persisted-viewport branch is OFF — once the
 * operator has saved their own pan/zoom, we never auto-refit on top of
 * it (matches the ``fitView=false`` guard in the parent ReactFlow).
 */
function RefitOnDetailChange({
  detailLevel,
  enabled,
}: {
  detailLevel: DetailLevel;
  enabled: boolean;
}): JSX.Element | null {
  const { fitView } = useReactFlow();
  // Track the previous level via ref so we ONLY refit on actual
  // transitions, not on every re-render that happens to carry the
  // current level.
  const prev = useRef<DetailLevel | null>(null);
  useEffect(() => {
    if (!enabled) {
      prev.current = detailLevel;
      return;
    }
    if (prev.current === detailLevel) return;
    prev.current = detailLevel;
    // Defer to the next frame so the layout swap (which is a separate
    // React state) has committed and the new node positions exist
    // before we measure their bbox.
    const handle = requestAnimationFrame(() => {
      // minZoom mirrors the floor pinned on the ReactFlow root so a
      // detail-level flip never refits us BELOW the threshold (which
      // would re-trigger the compact transition and oscillate).
      fitView({ padding: 0.05, includeHiddenNodes: false, minZoom: 0.45, maxZoom: 1.5 });
    });
    return () => cancelAnimationFrame(handle);
  }, [detailLevel, enabled, fitView]);
  return null;
}

/**
 * Run dagre layered layout to position nodes. Returns a fresh node
 * array with positions filled in. Idempotent — re-running on already-
 * positioned nodes produces the same result.
 *
 * W20 tuning: the previous nodesep=40 / ranksep=80 (with LR default)
 * produced layouts where a 15-state FSM (skill-code-review) sprawled
 * 6000+ px wide off-screen and predicate labels (e.g. `tier ==
 * 'trivial' AND len(risk_signals) == 0 AND NOT scope_overrides_...`)
 * crossed over unrelated nodes. The new defaults move FSMs to a TB
 * (top-to-bottom) layout that fits the viewport vertically and gives
 * dagre enough lateral slack to route long-predicate edges around
 * sibling nodes:
 *
 *   nodesep: 70      horizontal gap between sibling nodes in same rank (TB)
 *   ranksep: 90      vertical gap between consecutive ranks (TB)
 *   edgesep: 30      minimum gap between adjacent edges
 *   marginx: 30      graph padding left/right
 *   marginy: 30      graph padding top/bottom
 *   ranker: 'tight-tree'  prefers compact ranks over absolute shortest
 *                         paths; for FSMs with branchy predicates this
 *                         keeps the visual centre line stable
 *
 * Edge labels reserve dagre space proportional to text length (see
 * g.setEdge below) so dagre routes around them. Labels themselves are
 * truncated by decorateEdges to LABEL_MAX_CHARS so they stay legible
 * inside the reserved LABEL_WIDTH box.
 *
 * The graph is constructed as a multigraph because specToGraph can
 * legitimately emit two transitions with the same (source, target)
 * pair (e.g. a deterministic predicate plus an `otherwise` fallback
 * between the same two states). A non-multigraph Graph would silently
 * collapse them under `setEdge(v, w, ...)` and lose layout slack for
 * the dropped label. Multigraph mode requires a per-edge `name` so
 * dagre distinguishes parallel edges; we pass the React Flow edge id.
 */
// LABEL_PILL_MAX_WIDTH must match the FsmEdge pill's `max-w-[170px]`
// so dagre reserves slack matching the actual rendered width.
const LABEL_PILL_MAX_WIDTH = 170;
// Width per glyph for the 10px non-monospace UI font used inside the
// FsmEdge pill. The previous 7px estimate UNDERshot real rendered
// width because the syntax-coloured token spans use a mix of bold +
// proportional glyphs. 8px per char is a safe ceiling.
const LABEL_CHAR_WIDTH = 8;
// Horizontal padding (px-1.5 each side = 6px each, plus the SVG
// labelBgPadding of 6px) and the FsmEdge border. Dagre needs to know
// the FULL outer box, not just the text run.
const LABEL_HORIZONTAL_PADDING = 16;
// Line-height for the wrapped pill (in px) + 8px vertical pill padding.
const LABEL_LINE_HEIGHT = 14;
const LABEL_PILL_VERTICAL_PADDING = 12;
// Perpendicular distance dagre keeps between the label centre and the
// rank centreline. Default 10 lands labels right on the edge polyline;
// 24 lifts them off so they don't collide with adjacent node corners.
const LABEL_OFFSET = 24;

/**
 * Measure an edge label as the FsmEdge pill would render it. Returns
 * the outer box dagre should reserve so neighbouring edges/nodes don't
 * collide with the label.
 */
function measureEdgeLabel(text: string): { width: number; height: number } {
  if (!text) return { width: 0, height: 0 };
  const naiveWidth = text.length * LABEL_CHAR_WIDTH + LABEL_HORIZONTAL_PADDING;
  const width = Math.min(LABEL_PILL_MAX_WIDTH, Math.max(60, naiveWidth));
  // Lines = ceil(natural text width / usable width), where usable
  // width excludes horizontal padding. Worst-case break-words wrapping
  // matches the FsmEdge `whitespace-normal break-words` pill.
  const usable = Math.max(1, width - LABEL_HORIZONTAL_PADDING);
  const lines = Math.max(
    1,
    Math.ceil((text.length * LABEL_CHAR_WIDTH) / usable),
  );
  return { width, height: lines * LABEL_LINE_HEIGHT + LABEL_PILL_VERTICAL_PADDING };
}

/** Map from edge id -> dagre-assigned label centre coords (graph-space). */
export type DagreEdgeLabelMap = Map<string, { x: number; y: number }>;

function applyDagreLayout(
  nodes: readonly Node<FlowNodeData>[],
  edges: readonly Edge[],
  direction: 'LR' | 'TB',
  /** Default per-node dimensions used when the node didn't pre-stamp
   *  its own width/height. W23e: callers pass compact dims when the
   *  active viewport zoom drops below DETAIL_LEVEL_ZOOM_THRESHOLD so
   *  the layout packs the chain tighter. */
  defaultDims: { width: number; height: number } = NODE_DIMS_BY_DETAIL.full,
): { positioned: Node<FlowNodeData>[]; edgeLabelPositions: DagreEdgeLabelMap } {
  const g = new dagre.graphlib.Graph({ multigraph: true });
  g.setDefaultEdgeLabel(() => ({}));
  // W23c user correction: bias the layout HORIZONTAL. The previous
  // adaptive ranksep heuristic stretched the graph vertically until the
  // operator had to scroll on a 16:9 viewport. The fix is to widen
  // sibling spacing (nodesep) so predicate pills at the same rank stop
  // overlapping, and SHRINK ranksep so consecutive ranks pack tighter
  // vertically. Constants only; no label-aware logic this round.
  //
  //   nodesep: 280  horizontal gap between sibling nodes in same rank
  //   ranksep: 120  vertical gap between consecutive ranks (smaller)
  //   edgesep:  72  minimum gap between adjacent edges
  // TB layout for 15-state chain with branches: wide horizontal slack
  // (nodesep) keeps sibling predicate pills from stacking; ranks tight
  // since longest-path collapses straight chains and we rely on fitView
  // to scale the result. Predicate pills are anchored on cross-segment
  // midpoints so a tight ranksep is fine — adjacent ranks' labels live
  // on different horizontal segments and don't touch.
  // Clean-slate constants: restore dagre defaults that work for moderate
  // graphs (15-50 states). Previous tuning (nodesep=130 / ranksep=50)
  // came from over-fitting to a 15-node v1 spec without the loop node;
  // it collapsed adjacent ranks so far that predicate pills landed on
  // top of node corners as soon as the spec grew. The conservative
  // numbers below give dagre room to route around long edge labels and
  // keep the graph readable without sprawling off the viewport — we
  // rely on fitView (padding 0.15) to scale the final box.
  g.setGraph({
    rankdir: direction,
    nodesep: 120,
    ranksep: 80,
    edgesep: 36,
    marginx: 20,
    marginy: 20,
    ranker: 'network-simplex',
  });
  for (const n of nodes) {
    // PR 5: respect per-node width/height when the caller already
    // stamped them (loop nodes do — their card width depends on the
    // iteration count, which the run-overlay layer knows BEFORE the
    // layout pass). Fall back to the detail-level defaults for every
    // other node (W23e: defaults flip between full + compact depending
    // on the active viewport zoom).
    const w = typeof n.width === 'number' ? n.width : defaultDims.width;
    const h = typeof n.height === 'number' ? n.height : defaultDims.height;
    g.setNode(n.id, { width: w, height: h });
  }
  for (const e of edges) {
    const text = typeof e.label === 'string' ? e.label : '';
    // Reserve label space proportionally so dagre routes around
    // long predicate labels instead of letting them collide with
    // nodes downstream. measureEdgeLabel models the FsmEdge pill
    // (8px-per-char, horizontal padding, wrap @ 280px max-width) so
    // dagre's slack reflects the ACTUAL rendered box. The fourth
    // argument is the edge name; under multigraph mode it disambiguates
    // parallel edges so a second predicate between the same two states
    // doesn't clobber the first.
    const { width, height } = measureEdgeLabel(text);
    g.setEdge(
      e.source,
      e.target,
      {
        width,
        height,
        labelpos: 'c',
        // labeloffset pushes labels perpendicular to the rank
        // centreline. The dagre default (10) sits the label right on
        // the polyline, where it collides with adjacent node corners
        // when ranksep is tight. 24 lifts it clear.
        labeloffset: LABEL_OFFSET,
        // Long predicates get an extra rank of vertical slack so dagre
        // has somewhere to route the multi-line pill without stealing
        // space from siblings.
        minlen: text.length > 60 ? 2 : 1,
      },
      e.id,
    );
  }
  dagre.layout(g);

  // Capture dagre's per-edge label centre so FsmEdge can prefer the
  // layout-computed position over the geometric longest-segment
  // midpoint. Without this, siblings fanning out of one source land
  // their labels on the same midpoint band and visually collide even
  // though dagre's routing accounted for them. Multigraph mode requires
  // the {v, w, name} signature to retrieve the right parallel edge.
  const edgeLabelPositions: DagreEdgeLabelMap = new Map();
  for (const e of edges) {
    try {
      const de = g.edge({ v: e.source, w: e.target, name: e.id });
      if (de && typeof de.x === 'number' && typeof de.y === 'number') {
        edgeLabelPositions.set(e.id, { x: de.x, y: de.y });
      }
    } catch {
      // Edge not present (defensive: nothing to do).
    }
  }

  const positioned = nodes.map((n) => {
    const { x, y } = g.node(n.id);
    const w = typeof n.width === 'number' ? n.width : defaultDims.width;
    const h = typeof n.height === 'number' ? n.height : defaultDims.height;
    // PR 5: dispatch to the loop custom node type for loop states.
    // The discriminator is data.isLoop — set by specGraph for loop
    // bodies. Every other node keeps the existing fsmNode renderer.
    const isLoop = (n.data as { isLoop?: boolean } | undefined)?.isLoop === true;
    return {
      ...n,
      position: { x: x - w / 2, y: y - h / 2 },
      // xyflow v12 MiniMap reads node.width / node.height to draw the
      // thumbnail rectangle. Without these, the mini-map shows only the
      // viewport indicator + grid, no node dots (W22 user-visible bug).
      // Stamping the same dimensions we already feed dagre keeps the
      // mini-map in lockstep with the on-canvas layout.
      width: w,
      height: h,
      sourcePosition: direction === 'LR' ? Position.Right : Position.Bottom,
      targetPosition: direction === 'LR' ? Position.Left : Position.Top,
      type: isLoop ? 'loopNode' : 'fsmNode',
    };
  });
  return { positioned, edgeLabelPositions };
}

// W23b regression fix: do NOT pre-truncate edge labels. The FsmEdge
// custom edge type owns visual presentation (a 280px max-width pill
// with break-words wrapping) and surfaces the full predicate text via
// hover Tooltip. Pre-truncating here would just clip the visible text
// before the pill ever got the chance to wrap it. The dagre slack
// reservation in applyDagreLayout still uses the original label
// length so routing stays clear of long predicates.
function truncateLabel(text: string): string {
  return text;
}

// Decorate edges so they render visibly in both themes: stroke via
// currentColor (the outer container sets text-color per theme), arrow
// markers so direction is unambiguous, labels with a themed fill +
// solid background rect so the text is always legible against the
// graph background.
function decorateEdges(
  edges: readonly Edge[],
  edgeLabelPositions?: DagreEdgeLabelMap,
  elkEdgeRouting?: Map<string, ElkEdgeRouting>,
): Edge[] {
  return edges.map((e) => {
    const original = typeof e.label === 'string' ? e.label : undefined;
    const labelText = typeof e.label === 'string' ? truncateLabel(e.label) : e.label;
    // Carry the full predicate + transition metadata on edge.data so the
    // custom FsmEdge type can Tooltip it AND so onEdgeClick consumers
    // (specDetail's inspector Sheet) can render the full payload without
    // re-walking the spec.
    const incomingData = (e.data ?? {}) as Record<string, unknown>;
    const labelPos = edgeLabelPositions?.get(e.id);
    const elkRouting = elkEdgeRouting?.get(e.id);
    const data: Record<string, unknown> = {
      ...incomingData,
      fullLabel: typeof incomingData.fullLabel === 'string'
        ? incomingData.fullLabel
        : original,
      sourceId: e.source,
      targetId: e.target,
      // layoutLabel propagates the active-layout-engine's label centre
      // to FsmEdge, which prefers it over the geometric longest-segment
      // midpoint when present. This is what stops sibling labels on a
      // fan-out from stacking on the same midpoint band. Same field name
      // for both ELK and dagre; FsmEdge does not care which engine
      // produced it.
      ...(labelPos ? { layoutLabel: labelPos } : {}),
      // dagreLabel kept as a backwards-compat alias for any FsmEdge
      // instance that didn't pick up the renamed field (e.g. a stale
      // tab on a SW-cached build).
      ...(labelPos ? { dagreLabel: labelPos } : {}),
      // elkSections, when present, instructs FsmEdge to build its SVG
      // path from the ELK orthogonal polyline with rounded corners
      // instead of calling getSmoothStepPath.
      ...(elkRouting && elkRouting.sections.length > 0
        ? { elkSections: elkRouting.sections }
        : {}),
    };
    return {
      ...e,
      // W21: use FsmEdge custom type which wraps the label in a Tooltip
      // and exposes the full predicate via hover/focus. Defaults to
      // 'step' visual routing under the hood (sharp 90-degree corners,
      // matches PR #56's orthogonal routing).
      type: e.type ?? 'fsmEdge',
      animated: e.animated ?? false,
      label: labelText,
      data,
      style: {
        strokeWidth: 1.5,
        stroke: 'currentColor',
        ...(e.style ?? {}),
      },
      markerEnd: e.markerEnd ?? {
        type: MarkerType.ArrowClosed,
        width: 18,
        height: 18,
        color: 'currentColor',
      },
      labelStyle: {
        fontSize: 10,
        fill: 'currentColor',
        ...(e.labelStyle ?? {}),
      },
      // Render a solid background rect under each label so an
      // edge that happens to pass close to another node still has
      // a readable label badge.
      labelShowBg: true,
      labelBgStyle: {
        fill: 'var(--xy-label-bg, #f8fafc)',
        fillOpacity: 0.92,
      },
      labelBgPadding: [6, 4] as [number, number],
      labelBgBorderRadius: 4,
    };
  });
}

export function FlowGraph({
  nodes,
  edges,
  autoLayout = true,
  direction = 'TB',
  onNodeClick,
  onEdgeClick,
  onIterationClick,
  selectedNodeId,
  miniMap,
  controls = true,
  background = true,
  className,
  viewportKey,
}: FlowGraphProps): JSX.Element {
  // W23b: viewport persistence when the caller opts in via viewportKey.
  // The hook returns undefined when nothing is stored yet, in which case
  // we keep the existing fitView default so first-paint frames the graph.
  const viewport = useGraphViewport(viewportKey ?? '');

  // Capture-phase wheel adapter so Shift+wheel pans horizontally even
  // though wheel events from a vertical mouse wheel carry only deltaY.
  // xyflow's pan-on-scroll handler consumes deltaY directly; without
  // swapping the axes here, Shift+wheel would still pan vertically.
  // Cmd/Ctrl+wheel zoom is left untouched (xyflow handles it via
  // zoomActivationKeyCode).
  const wrapperRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const el = wrapperRef.current;
    if (!el) return undefined;
    const onWheel = (e: WheelEvent): void => {
      if (e.metaKey || e.ctrlKey) return; // zoom path, let xyflow handle.
      if (e.shiftKey && e.deltaX === 0 && e.deltaY !== 0) {
        e.stopPropagation();
        e.preventDefault();
        el.dispatchEvent(
          new WheelEvent('wheel', {
            bubbles: true,
            cancelable: true,
            deltaX: e.deltaY,
            deltaY: 0,
            deltaZ: e.deltaZ,
            deltaMode: e.deltaMode,
            clientX: e.clientX,
            clientY: e.clientY,
            shiftKey: false,
          }),
        );
      }
    };
    el.addEventListener('wheel', onWheel, { capture: true, passive: false });
    return () => {
      el.removeEventListener('wheel', onWheel, { capture: true } as unknown as EventListenerOptions);
    };
  }, []);
  // W23d 1-hop hover highlight state. Tracking the hovered node / edge
  // here (not inside FsmNode / FsmEdge) keeps every node + edge aware
  // of what the operator is pointing at, which is what lets us dim
  // everything outside the 1-hop neighbourhood. Only one of these is
  // ever non-null at a time; entering an edge clears the node hover
  // and vice-versa to avoid an in-between flicker where both apply.
  const [hoveredNodeId, setHoveredNodeId] = useState<string | null>(null);
  const [hoveredEdgeId, setHoveredEdgeId] = useState<string | null>(null);

  // Pre-compute the "highlighted set" — the set of node ids and edge
  // ids that are 1 hop away from the current hover target. useMemo
  // keyed on edges + the hover ids so we don't rebuild the set on
  // every unrelated render (e.g. a viewport pan that re-renders the
  // FlowGraph but doesn't change the topology).
  const highlight = useMemo(() => {
    const highlightedNodes = new Set<string>();
    const highlightedEdges = new Set<string>();
    if (hoveredNodeId !== null) {
      highlightedNodes.add(hoveredNodeId);
      for (const e of edges) {
        if (e.source === hoveredNodeId || e.target === hoveredNodeId) {
          highlightedEdges.add(e.id);
          highlightedNodes.add(e.source);
          highlightedNodes.add(e.target);
        }
      }
    } else if (hoveredEdgeId !== null) {
      const hoveredEdge = edges.find((e) => e.id === hoveredEdgeId);
      if (hoveredEdge) {
        highlightedEdges.add(hoveredEdge.id);
        highlightedNodes.add(hoveredEdge.source);
        highlightedNodes.add(hoveredEdge.target);
      }
    }
    return { nodes: highlightedNodes, edges: highlightedEdges };
  }, [edges, hoveredNodeId, hoveredEdgeId]);

  const hoverActive = hoveredNodeId !== null || hoveredEdgeId !== null;

  // Detect the active layout engine from the URL. ?layout=dagre keeps
  // the legacy dagre path alive for A/B comparison + emergency rollback.
  // Any other value (or no query param) selects ELK, the new default.
  //
  // This lookup is intentionally read on EVERY render rather than wrapped
  // in `useMemo([])`. A memo with an empty dep array would capture
  // `window.location.search` once at mount and never see a mid-session
  // toggle (e.g. an operator navigating to `?layout=dagre` via the
  // History API without a full remount). React's docs are explicit:
  // useMemo is a perf hint, not a correctness mechanism; values that
  // depend on mutable globals must be derived during render so the
  // component stays consistent with the latest external state.
  // See https://react.dev/reference/react/useMemo#caveats.
  const layoutEngine: 'elk' | 'dagre' = (() => {
    if (typeof window === 'undefined') return 'elk';
    const params = new URLSearchParams(window.location.search);
    return params.get('layout') === 'dagre' ? 'dagre' : 'elk';
  })();

  // W23e: viewport-zoom-aware detail toggle. The card render path + the
  // layout pass both flip when the active zoom crosses
  // DETAIL_LEVEL_ZOOM_THRESHOLD. We default to 'full' on mount so the
  // first render (before any zoom is known) lines up with the
  // pre-W23e visual baseline; the ZoomDetailWatcher mounted inside
  // ReactFlow snaps us to 'compact' on the next frame when fitView
  // computes a zoom <= the threshold.
  const [detailLevel, setDetailLevel] = useState<DetailLevel>('full');

  // Manual-layout branch: preserve caller positions, stamp Top/Bottom
  // handle sides (TB layout is now pinned regardless of the `direction`
  // prop — see prop doc above), dispatch loop vs fsm node type.
  // Defaults flip with the detail level so a manual layout's nodes
  // still pick up the compact dims when active.
  const manualPositioned = useMemo(() => {
    if (autoLayout) return null;
    // Vertical orientation invariant: source handle anchors at the
    // bottom of the card, target handle at the top.
    void direction; // referenced for back-compat callers; value ignored.
    const sp = Position.Bottom;
    const tp = Position.Top;
    const dims = NODE_DIMS_BY_DETAIL[detailLevel];
    return nodes.map((n) => {
      const isLoop = (n.data as { isLoop?: boolean } | undefined)?.isLoop === true;
      return {
        ...n,
        width: n.width ?? dims.width,
        height: n.height ?? dims.height,
        sourcePosition: n.sourcePosition ?? sp,
        targetPosition: n.targetPosition ?? tp,
        type: n.type ?? (isLoop ? 'loopNode' : 'fsmNode'),
      };
    });
  }, [nodes, autoLayout, direction, detailLevel]);

  // Dagre auto-layout branch: synchronous and cheap (the existing
  // implementation). W23e: we cache BOTH detail-level layouts and pick
  // the one matching the active `detailLevel` so toggling zoom doesn't
  // re-pay the (sync, but visible) dagre cost. Direction is pinned to
  // 'TB' regardless of the prop so the dagre fallback matches the ELK
  // vertical-orientation invariant.
  const dagreLayouts = useMemo(() => {
    if (!autoLayout) return null;
    void direction; // back-compat only; layout is TB.
    return {
      full: applyDagreLayout(nodes, edges, 'TB', NODE_DIMS_BY_DETAIL.full),
      compact: applyDagreLayout(nodes, edges, 'TB', NODE_DIMS_BY_DETAIL.compact),
    };
  }, [nodes, edges, autoLayout, direction]);
  const dagreLayout = dagreLayouts?.[detailLevel] ?? null;

  // ELK layout is async (Promise<LayoutResult>). W23e: we hold one
  // result per detail level so the toggle switches between cached
  // layouts instantly. Both layouts kick off in parallel on the same
  // (nodes, edges, direction) change.
  const [elkLayouts, setElkLayouts] = useState<{
    full: LayoutResult | null;
    compact: LayoutResult | null;
  }>({ full: null, compact: null });
  const [isLayoutComputing, setIsLayoutComputing] = useState<boolean>(false);
  const [elkFailed, setElkFailed] = useState<boolean>(false);

  // Recompute ELK whenever the engine, nodes/edges identity, or
  // direction changes. We deliberately depend on `edges` and `nodes`
  // identity (not contents) because the upstream owners (specGraph,
  // runGraph) regenerate the arrays whenever the topology changes;
  // a stable identity means "same topology" and we can reuse the
  // previous ELK result.
  useEffect(() => {
    if (!autoLayout || layoutEngine !== 'elk') {
      setElkLayouts({ full: null, compact: null });
      setIsLayoutComputing(false);
      return;
    }
    let cancelled = false;
    setIsLayoutComputing(true);
    // Build the label-dimensions map from each edge's actual label
    // text (same measurement we feed dagre, so ELK reserves matching
    // slack for the pill).
    const labelDimensions = new Map<string, { width: number; height: number }>();
    for (const e of edges) {
      const text = typeof e.label === 'string' ? e.label : '';
      if (text.length > 0) labelDimensions.set(e.id, measureEdgeLabel(text));
    }
    // Stamp data.layoutWidth/layoutHeight per detail level so the ELK
    // wrapper (which reads those as a fallback) reserves the matching
    // slack. We rebuild the input arrays per level rather than mutate
    // the caller's nodes.
    const buildInput = (level: DetailLevel): Node<FlowNodeData>[] => {
      const dims = NODE_DIMS_BY_DETAIL[level];
      return nodes.map((n) => ({
        ...n,
        data: {
          ...n.data,
          // Only stamp the fallback when the node didn't already declare
          // explicit width/height. Loop nodes pre-W23e never did this
          // either, so the clean-slate uniform-width contract is intact.
          ...(typeof n.width === 'number'
            ? {}
            : { layoutWidth: dims.width, layoutHeight: dims.height }),
        } as FlowNodeData,
      }));
    };
    const fullPromise = applyElkLayout(buildInput('full'), edges, {
      labelDimensions,
      // Direction is pinned to DOWN inside the ELK wrapper itself;
      // omit it from the per-call override block. The ``direction``
      // prop only governs handle anchor sides (Top/Bottom) at the
      // React Flow layer.
      layoutOptions: {},
    });
    // Compact-mode layout: clamp the per-edge label box to a small
    // square (24x12) so ELK reserves enough slack to keep labels OFF
    // adjacent nodes, but doesn't blow the layer-to-layer gap out the
    // way the full-pill (~170x40) reservation does. The pills still
    // render at compact-mode font size (FsmEdge sizes them via
    // detailLevel) so they fit the slack we reserved.
    // Compact-mode labels render as a 14x14 pip (FsmEdge owns the
    // compact-mode visual), so we reserve a matching 14x14 box on the
    // layout side. Tight node-node spacing keeps the bbox compact so
    // fit-view zoom lands above 0.55 where the compact-mode card text
    // (truncated state names) is actually readable.
    const compactLabelDimensions = new Map<string, { width: number; height: number }>();
    for (const e of edges) {
      const text = typeof e.label === 'string' ? e.label : '';
      if (text.length > 0) compactLabelDimensions.set(e.id, { width: 14, height: 14 });
    }
    const compactPromise = applyElkLayout(buildInput('compact'), edges, {
      labelDimensions: compactLabelDimensions,
      layoutOptions: {
        // Direction pinned to DOWN inside the ELK wrapper; omit here.
        // Tight spacing — the compact pip + small card means we can
        // pack 14 layers into ~1500 px wide and clear fit-view 0.6+.
        // Tested with skill-code-review v4 (18 nodes / 14 layers LR):
        // these constants produce fit-view zoom ~0.6 on a 1330x780
        // viewport, putting the compact-mode 13px label text at ~7.8 px
        // on-screen which is the readability floor for the truncated
        // state names.
        'elk.layered.spacing.nodeNodeBetweenLayers': '10',
        'elk.layered.spacing.edgeNodeBetweenLayers': '6',
        'elk.spacing.nodeNode': '20',
        'elk.spacing.edgeLabel': '2',
        'elk.padding': '[top=4,left=4,bottom=4,right=4]',
      },
    });
    Promise.all([fullPromise, compactPromise])
      .then(([full, compact]) => {
        if (cancelled) return;
        setElkLayouts({ full, compact });
        setIsLayoutComputing(false);
        setElkFailed(false);
      })
      .catch((err) => {
        if (cancelled) return;
        // ELK throws are non-fatal: log, mark the fallback, render
        // through the existing dagre useMemo result.
        // eslint-disable-next-line no-console -- defensive diagnostic
        console.warn('ELK layout failed, falling back to dagre', err);
        setElkLayouts({ full: null, compact: null });
        setIsLayoutComputing(false);
        setElkFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, [nodes, edges, direction, autoLayout, layoutEngine]);

  const elkLayout = elkLayouts[detailLevel];

  // Resolve the active layout result to feed into the node/edge
  // decoration steps. Priority:
  //   1. Manual (autoLayout=false) — preserved positions + direction.
  //   2. ELK auto-layout, when result is available + not failed.
  //   3. Dagre auto-layout (legacy path OR ELK fallback).
  const { positioned, edgeLabelPositions, elkEdgeRouting } = useMemo(() => {
    if (manualPositioned !== null) {
      return {
        positioned: manualPositioned,
        edgeLabelPositions: new Map() as DagreEdgeLabelMap,
        elkEdgeRouting: new Map() as Map<string, ElkEdgeRouting>,
      };
    }
    const usingElk =
      layoutEngine === 'elk' && !elkFailed && elkLayout !== null;
    if (usingElk) {
      // Build positioned nodes from the ELK result. Vertical
      // orientation invariant: source handle at the bottom, target at
      // the top, matching the pinned 'elk.direction'='DOWN'.
      void direction;
      const sp = Position.Bottom;
      const tp = Position.Top;
      const dims = NODE_DIMS_BY_DETAIL[detailLevel];
      const elkNodes = nodes.map((n) => {
        const isLoop = (n.data as { isLoop?: boolean } | undefined)?.isLoop === true;
        const w = typeof n.width === 'number' ? n.width : dims.width;
        const h = typeof n.height === 'number' ? n.height : dims.height;
        const pos = elkLayout!.nodePositions.get(n.id) ?? { x: 0, y: 0 };
        return {
          ...n,
          // ELK returns top-left coords directly; React Flow expects
          // the same. No centre-then-offset arithmetic like the dagre
          // path requires.
          position: { x: pos.x, y: pos.y },
          width: w,
          height: h,
          sourcePosition: sp,
          targetPosition: tp,
          type: isLoop ? 'loopNode' : 'fsmNode',
        };
      });
      // Surface per-edge label positions through the same
      // edgeLabelPositions map dagre uses, so decorateEdges can
      // continue stamping data.layoutLabel uniformly.
      const labelPositions: DagreEdgeLabelMap = new Map();
      for (const [id, routing] of elkLayout!.edgeRouting) {
        if (routing.labelPos) labelPositions.set(id, routing.labelPos);
      }
      return {
        positioned: elkNodes,
        edgeLabelPositions: labelPositions,
        elkEdgeRouting: elkLayout!.edgeRouting,
      };
    }
    // Fall through to dagre.
    const dagre = dagreLayout ?? { positioned: [], edgeLabelPositions: new Map() };
    return {
      positioned: dagre.positioned,
      edgeLabelPositions: dagre.edgeLabelPositions,
      elkEdgeRouting: new Map() as Map<string, ElkEdgeRouting>,
    };
  }, [
    manualPositioned,
    dagreLayout,
    elkLayout,
    elkFailed,
    layoutEngine,
    nodes,
    direction,
    detailLevel,
  ]);

  const decoratedNodes = useMemo(
    () =>
      positioned.map((n) => {
        const isHovered = hoveredNodeId === n.id;
        const isHighlighted = !isHovered && highlight.nodes.has(n.id);
        const isDimmed = hoverActive && !isHovered && !isHighlighted;
        // Spread node.data so we don't drop any caller-set fields
        // (kind, label, runStatus, isCurrent, etc.). The three hover
        // flags are stripped back to undefined when nothing is hovered
        // so FsmNode's `=== true` checks render the un-highlighted
        // baseline. detailLevel is stamped here (not on the source
        // nodes array) so the toggle is a cheap re-decorate; the
        // upstream owners stay detail-level-agnostic.
        const nextData = {
          ...n.data,
          isHovered,
          highlighted: isHighlighted,
          dimmed: isDimmed,
          detailLevel,
        } as FlowNodeData;
        const withSelection =
          selectedNodeId && n.id === selectedNodeId ? { ...n, selected: true } : n;
        return { ...withSelection, data: nextData };
      }),
    [positioned, selectedNodeId, hoveredNodeId, hoverActive, highlight, detailLevel],
  );

  const showMiniMap = miniMap ?? nodes.length > 10;

  // Compute decorated edges BEFORE any conditional return. React's
  // rules-of-hooks require every hook to be called in the same order
  // on every render; the previous version put this useMemo after the
  // empty-graph early return, which threw when the same FlowGraph
  // flipped between 0 nodes and >0 nodes (Copilot finding on PR #57).
  const decoratedEdges = useMemo(() => {
    const base = decorateEdges(edges, edgeLabelPositions, elkEdgeRouting);
    return base.map((e) => {
      const isHovered = hoveredEdgeId === e.id;
      const isHighlighted = !isHovered && highlight.edges.has(e.id);
      const isDimmed = hoverActive && !isHovered && !isHighlighted;
      const nextData = {
        ...(e.data ?? {}),
        isHovered,
        highlighted: isHighlighted,
        dimmed: isDimmed,
        // Mirror the node-side detailLevel onto every edge so FsmEdge
        // can shrink (or hide) its label pill when the graph is
        // rendering compact cards. Predicate labels at compact zoom
        // overflow node footprints and undo the layout's slack
        // reservation, so the compact path drops to a small icon.
        detailLevel,
      };
      // Boost the SVG stroke when the edge is the hover target or a
      // 1-hop neighbour. FsmEdge owns label pill opacity / colour via
      // nextData; the path's stroke-width lives on style and is set
      // here because BaseEdge consumes it directly.
      const baseStrokeWidth =
        typeof (e.style as { strokeWidth?: number } | undefined)?.strokeWidth === 'number'
          ? ((e.style as { strokeWidth?: number }).strokeWidth as number)
          : 1.5;
      const nextStrokeWidth = isHovered
        ? Math.max(baseStrokeWidth, 2.5)
        : isHighlighted
        ? Math.max(baseStrokeWidth, 2)
        : baseStrokeWidth;
      const nextStyle = {
        ...(e.style ?? {}),
        strokeWidth: nextStrokeWidth,
        opacity: isDimmed ? 0.3 : 1,
        // The path doesn't need a CSS class hook for the transition —
        // BaseEdge renders a plain <path>. xyflow applies CSS via
        // style only, so we set the transition inline.
        transition: 'opacity 150ms ease-out, stroke-width 150ms ease-out',
      };
      return { ...e, data: nextData, style: nextStyle };
    });
  }, [edges, edgeLabelPositions, elkEdgeRouting, hoveredEdgeId, hoverActive, highlight, detailLevel]);

  // React Flow's onNodeMouseEnter / Leave fire with (event, node);
  // collapse to just the id and clear any active edge hover so the two
  // hover states stay mutually exclusive.
  const onNodeMouseEnter = useCallback((_e: unknown, node: { id: string }) => {
    setHoveredNodeId(node.id);
    setHoveredEdgeId(null);
  }, []);
  const onNodeMouseLeave = useCallback(() => {
    setHoveredNodeId(null);
  }, []);
  const onEdgeMouseEnter = useCallback((_e: unknown, edge: { id: string }) => {
    setHoveredEdgeId(edge.id);
    setHoveredNodeId(null);
  }, []);
  const onEdgeMouseLeave = useCallback(() => {
    setHoveredEdgeId(null);
  }, []);

  // Stable callback so ZoomDetailWatcher's useEffect doesn't re-fire on
  // every parent render. setState skips a render when the next value is
  // identical, so we don't have to guard here.
  const handleDetailLevelChange = useCallback((next: DetailLevel) => {
    setDetailLevel(next);
  }, []);

  if (nodes.length === 0) {
    return (
      <div
        class={[
          'flow-graph flex items-center justify-center text-sm text-slate-500',
          'h-64 border border-slate-200 dark:border-slate-700 rounded-md',
          className ?? '',
        ].join(' ')}
      >
        No graph data
      </div>
    );
  }

  // min-h-[320px] keeps the graph readable when the parent container
  // doesn't supply an explicit height. h-full + w-full make sure
  // xyflow's renderer has a measurable box when the parent DOES set
  // a height (e.g. the Specs route's h-[60vh] tab panel).
  //
  // The wrapper's `text-slate-400 dark:text-slate-600` drives edge
  // stroke colour via currentColor (see decorateEdges). The fitView
  // padding adds breathing room so dagre's wide LR layout doesn't
  // clip nodes at the viewport edges.
  return (
    <FsmEdgeClickContext.Provider value={onEdgeClick ?? null}>
    <LoopIterationClickContext.Provider value={onIterationClick ?? null}>
    <div
      ref={wrapperRef}
      class={[
        'flow-graph relative h-full w-full min-h-[320px]',
        'text-slate-400 dark:text-slate-500',
        className ?? '',
      ].join(' ')}
      data-layout-engine={layoutEngine}
      data-layout-fallback={elkFailed ? 'dagre' : undefined}
    >
      <ReactFlow
        nodes={decoratedNodes}
        edges={decoratedEdges}
        nodeTypes={NODE_TYPES}
        edgeTypes={EDGE_TYPES}
        // W23b: only fitView when no persisted viewport exists. Once
        // the operator has zoomed/panned and the viewport is saved,
        // restoring it on remount beats re-framing the whole graph.
        fitView={viewport.defaultViewport === undefined}
        // Min-zoom floor is 0.45 so the equilibrium fit-view zoom sits
        // ABOVE DETAIL_LEVEL_ZOOM_THRESHOLD (0.4). Without this floor a
        // tall TB chain (e.g. 14-layer skill-code-review v4) framed by
        // fitView lands at ~0.55-0.65 zoom, then ZoomDetailWatcher flips
        // detailLevel='compact' and the labels collapse to pips —
        // perceptually "min zoom is too low". With minZoom=0.45 the
        // fitView is clamped above the compact threshold and the graph
        // renders at full detail by default; the operator pans (drag or
        // wheel) to navigate a long chain. Cmd/Ctrl+wheel still zooms
        // in to maxZoom=2 if they want detail.
        fitViewOptions={{ padding: 0.05, includeHiddenNodes: false, minZoom: 0.45, maxZoom: 1.5 }}
        minZoom={0.45}
        maxZoom={2}
        defaultViewport={viewport.defaultViewport}
        onMove={viewportKey ? viewport.onMove : undefined}
        // Wheel = pan vertically (xyflow's panOnScroll). The wrapper
        // useEffect above swaps deltaY into deltaX when Shift is held
        // so Shift+wheel pans horizontally on plain mice. Cmd / Ctrl +
        // wheel forces zoom; pinch zoom keeps working on trackpads.
        // Drag also pans.
        panOnScroll
        panOnScrollMode={PanOnScrollMode.Free}
        zoomOnScroll={false}
        zoomOnPinch
        panOnDrag
        zoomActivationKeyCode={['Meta', 'Control']}
        selectionOnDrag={false}
        proOptions={{ hideAttribution: true }}
        defaultEdgeOptions={{
          // Edges that bypass decorateEdges (e.g. ad-hoc test fixtures)
          // fall back to plain 'step' routing instead of fsmEdge so
          // they don't break in absence of FsmEdgeData on data.
          type: 'step',
          style: { stroke: 'currentColor', strokeWidth: 1.5 },
          markerEnd: {
            type: MarkerType.ArrowClosed,
            width: 18,
            height: 18,
            color: 'currentColor',
          },
        }}
        onNodeClick={(_e, node) => onNodeClick?.(node.id, node.data as FlowNodeData)}
        onEdgeClick={(_e, edge) =>
          onEdgeClick?.(edge.id, edge.data as Record<string, unknown> | undefined)
        }
        onNodeMouseEnter={onNodeMouseEnter}
        onNodeMouseLeave={onNodeMouseLeave}
        onEdgeMouseEnter={onEdgeMouseEnter}
        onEdgeMouseLeave={onEdgeMouseLeave}
      >
        {/* W23e: lives inside ReactFlow so useStore has a real store
            context. Renders nothing — its only job is to subscribe to
            the live viewport zoom and lift a detailLevel up to the
            orchestrator when the value crosses the threshold. */}
        <ZoomDetailWatcher onChange={handleDetailLevelChange} />
        <RefitOnDetailChange
          detailLevel={detailLevel}
          enabled={viewport.defaultViewport === undefined}
        />
        {background ? (
          <Background
            gap={16}
            // Subtle dot pattern; the CSS sets dot colour based on
            // current text colour so both themes render visibly.
            color="currentColor"
            variant={BackgroundVariant.Dots}
          />
        ) : null}
        {controls ? (
          <Controls
            position="bottom-right"
            showInteractive={false}
            // Override default white background; let xyflow inherit
            // surface colours from theme tokens via Tailwind.
            className="!bg-white dark:!bg-slate-800 !border !border-slate-200 dark:!border-slate-700 !rounded-md !overflow-hidden [&_button]:!bg-white [&_button]:dark:!bg-slate-800 [&_button]:!text-slate-700 [&_button]:dark:!text-slate-200 [&_button]:!border-slate-200 [&_button]:dark:!border-slate-700 [&_button:hover]:!bg-slate-100 [&_button:hover]:dark:!bg-slate-700"
          />
        ) : null}
        {showMiniMap ? (
          <MiniMap
            pannable
            zoomable
            position="top-right"
            // Theme-aware mini-map: dim background, themed node colour.
            maskColor="rgba(15, 23, 42, 0.05)"
            nodeColor={(n) => {
              const k = (n.data as FlowNodeData | undefined)?.kind;
              switch (k) {
                case 'worker': return '#0ea5e9';
                case 'inline': return '#8b5cf6';
                case 'terminal': return '#64748b';
                case 'producer': return '#f59e0b';
                case 'consumer': return '#06b6d4';
                default: return '#10b981';
              }
            }}
            className="!bg-white/80 dark:!bg-slate-800/80 !border !border-slate-200 dark:!border-slate-700 !rounded-md"
          />
        ) : null}
      </ReactFlow>
      {/* ELK layout overlay. The compute usually completes in 50-300 ms;
          on the first paint the spinner sits above the canvas until the
          ELK promise resolves. honour prefers-reduced-motion by letting
          Spinner's animate-spin class collapse via the global rule. */}
      {isLayoutComputing ? (
        <div
          class={[
            'fsm-graph-spinner absolute inset-0 z-20 flex items-center justify-center',
            'bg-white/60 dark:bg-slate-900/60',
            'pointer-events-none',
          ].join(' ')}
          data-testid="fsm-graph-spinner"
          role="status"
          aria-live="polite"
        >
          <Spinner size="lg" label="Computing graph layout" />
        </div>
      ) : null}
      {/* W23b: discoverable wheel-mode help. Floating in the top-left
          (mirrors the bottom-right Controls position). Hover opens a
          small keybindings card so the operator who didn't already
          know "Shift+wheel pans" learns it. */}
      <div class="absolute top-2 left-2 z-10 pointer-events-auto">
        <Tooltip
          content={
            <div class="text-left text-xs leading-relaxed">
              <div class="font-semibold mb-1">Graph controls</div>
              <div>Wheel: scroll vertically.</div>
              <div>Shift + Wheel: scroll horizontally.</div>
              <div>Cmd/Ctrl + Wheel: zoom.</div>
              <div>Drag: pan.</div>
              <div>Double-click: fit.</div>
            </div>
          }
          delay={200}
        >
          <button
            type="button"
            class={[
              'flex h-7 w-7 items-center justify-center rounded-md',
              'border border-slate-200 dark:border-slate-700',
              'bg-white dark:bg-slate-800',
              'text-slate-600 dark:text-slate-300',
              'hover:bg-slate-100 dark:hover:bg-slate-700',
              'focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500',
              'text-sm font-semibold',
            ].join(' ')}
            aria-label="Graph controls help"
          >
            ?
          </button>
        </Tooltip>
      </div>
    </div>
    </LoopIterationClickContext.Provider>
    </FsmEdgeClickContext.Provider>
  );
}

export default FlowGraph;
