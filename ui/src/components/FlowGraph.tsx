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

import { useCallback, useContext, useEffect, useMemo, useRef, useState } from 'preact/hooks';
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
  type Edge,
  type Node,
  type NodeProps,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';

import { useGraphViewport } from '../lib/graphViewport';
import { Tooltip } from './Tooltip';
import { FsmEdge, FsmEdgeClickContext } from './FsmEdge';

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
  /** Layout direction. Default 'TB' (top to bottom) — natural reading for
   * FSMs, and fits a viewport-bounded panel better than LR for long chains. */
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

// W21 user-requested: 50% bigger than the W20 sizes (180x56 → 270x84)
// so long state ids (e.g. `synthesize_release_readiness`) fit on one
// line and the kind badge no longer competes for horizontal room.
// Dagre layout constants below are tuned proportionally to keep edges
// clear of the larger node boxes.
const NODE_WIDTH = 270;
const NODE_HEIGHT = 84;

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

function FsmNode({ data, selected, sourcePosition, targetPosition }: NodeProps<Node<FlowNodeData>>): JSX.Element {
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
  return (
    <div
      class={[
        'fsm-node relative rounded-md border-2 shadow-sm px-4 py-3',
        // justify-center centers the kind/label block vertically within
        // the (fixed) node height. gap-1 gives the two rows even
        // breathing room.
        'flex flex-col gap-1 justify-center overflow-hidden',
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
      style={{ width: `${NODE_WIDTH}px`, minHeight: `${NODE_HEIGHT}px` }}
    >
      <Handle
        type="target"
        position={tp}
        style={{ background: 'currentColor', width: 8, height: 8, border: 'none' }}
      />
      <span class="text-[9px] uppercase tracking-wider opacity-60 leading-none">
        {kind}
      </span>
      <Tooltip content={data.fullLabel ?? data.label} delay={400}>
        <span class="font-semibold text-sm truncate block w-full leading-tight">
          {typeof data.labelPrefix === 'string' ? data.labelPrefix : ''}
          {data.label}
        </span>
      </Tooltip>
      <Handle
        type="source"
        position={sp}
        style={{ background: 'currentColor', width: 8, height: 8, border: 'none' }}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// PR 5: loop node — header card + collapsible chip strip
// ---------------------------------------------------------------------------

/**
 * Width of the header band of a loop node (kind chip + state-id + ×N
 * badge + expand toggle). Matches the default FsmNode card so a loop
 * node collapsed reads at the same line height as its worker / inline
 * siblings on the same rank.
 */
export const LOOP_NODE_HEADER_WIDTH = 270;
/** Width of one iteration chip inside the strip. */
export const LOOP_NODE_CHIP_WIDTH = 40;
/** Hard cap on the number of chips contributed to the layout width;
 *  beyond this the strip uses horizontal scroll so a 200-iteration
 *  loop doesn't sprawl 8000px wide and break dagre. */
export const LOOP_NODE_CHIP_VISIBLE_MAX = 20;
/** Total node height (matches FsmNode for collapsed parity, plus a
 *  fixed chip-strip height that's allocated whether the strip is
 *  visible or not so the layout doesn't shift when the operator
 *  expands). */
export const LOOP_NODE_HEIGHT = NODE_HEIGHT + 36;

/**
 * Compute the layout width of a loop node given an iteration count.
 * Exposed so ``specGraph`` / ``runGraph`` can stamp the same number
 * onto the node BEFORE the dagre layout pass runs — that keeps dagre
 * from re-routing edges every time the operator collapses the chip
 * strip.
 */
export function loopNodeExpandedWidth(iterations: number): number {
  const visible = Math.min(LOOP_NODE_CHIP_VISIBLE_MAX, Math.max(0, iterations));
  return LOOP_NODE_HEADER_WIDTH + visible * LOOP_NODE_CHIP_WIDTH;
}

/** Per-status chip palette inside the loop strip. */
const LOOP_CHIP_STATUS_CLASSES: Record<string, string> = {
  faulted:
    'border-red-500 bg-red-100 dark:bg-red-900/50 text-red-900 dark:text-red-100',
  entered:
    'border-amber-500 bg-amber-100 dark:bg-amber-900/50 text-amber-900 dark:text-amber-100',
  exited:
    'border-emerald-500 bg-emerald-100 dark:bg-emerald-900/50 text-emerald-900 dark:text-emerald-100',
  completed:
    'border-emerald-500 bg-emerald-100 dark:bg-emerald-900/50 text-emerald-900 dark:text-emerald-100',
};

function loopChipClass(status: string): string {
  return (
    LOOP_CHIP_STATUS_CLASSES[status.toLowerCase()] ??
    'border-slate-400 bg-slate-100 dark:bg-slate-800/60 text-slate-700 dark:text-slate-300'
  );
}

/** Context bridge for chip click dispatch.
 *  Mirrors the FsmEdgeClickContext pattern (the loop node renderer is
 *  instantiated by xyflow inside a portal, so prop-drilling
 *  ``onIterationClick`` through the nodeTypes map would lose it). */
export const LoopIterationClickContext = createContext<
  ((entryId: string) => void) | null
>(null);

function LoopNode({
  data,
  selected,
  sourcePosition,
  targetPosition,
}: NodeProps<Node<FlowNodeData>>): JSX.Element {
  const sp = sourcePosition ?? Position.Bottom;
  const tp = targetPosition ?? Position.Top;
  const onIterationClick = useContext(LoopIterationClickContext);

  // PR 5: the operator controls expand/collapse with the ▸ toggle. The
  // default is collapsed so a graph full of loop nodes doesn't drown
  // the operator in chip strips before they ask for one; expanding is
  // one keypress / click away. State lives in the node component (not
  // on data) so toggling doesn't trigger an overlay rebuild.
  const [expanded, setExpanded] = useState(false);

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

  // Reuse the status palette FsmNode owns for the header band so a
  // loop card lights up the same emerald/amber/red as its worker
  // siblings under the same run state.
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

  // The card width matches the layout-baked value (header +
  // min(MAX, iterations) chips) so dagre's slack remains correct.
  // The chip strip itself is independently overflow-x:auto, which
  // means an over-MAX iteration count scrolls horizontally inside
  // the fixed card width.
  const cardWidth = loopNodeExpandedWidth(Math.max(maxIterations, iterationCount));

  return (
    <div
      class={[
        'fsm-node fsm-loop-node relative rounded-md border-2 shadow-sm',
        'flex flex-col overflow-hidden',
        'transition-opacity duration-150 motion-reduce:transition-none',
        paletteClass,
        selected ? 'ring-2 ring-emerald-400 ring-offset-1' : '',
        currentRing,
        currentPulse,
        hoverRing,
        dimClass,
      ].join(' ')}
      data-testid="loop-node"
      /* eslint-disable-next-line react/forbid-dom-props -- xyflow loop node needs computed pixel width matched to dagre layout */
      style={{ width: `${cardWidth}px`, minHeight: `${LOOP_NODE_HEIGHT}px` }}
    >
      <Handle
        type="target"
        position={tp}
        style={{ background: 'currentColor', width: 8, height: 8, border: 'none' }}
      />
      {/* Header band — kind chip + label + ×N badge + expand toggle */}
      <div class="flex flex-col gap-1 px-4 py-3">
        <div class="flex items-center justify-between gap-2">
          <span class="text-[9px] uppercase tracking-wider opacity-60 leading-none">
            loop
          </span>
          <div class="flex items-center gap-1">
            <span
              class="text-[10px] font-mono px-1.5 py-0.5 rounded bg-black/10 dark:bg-white/10"
              title={`${iterationCount} of ${maxIterations} iterations`}
              data-testid="loop-iteration-badge"
            >
              ×{iterationCount}
              {maxIterations > 0 ? ` / ${maxIterations}` : ''}
            </span>
            <button
              type="button"
              aria-expanded={expanded ? 'true' : 'false'}
              aria-label={
                expanded ? 'Collapse iteration chips' : 'Expand iteration chips'
              }
              data-testid="loop-expand-toggle"
              onClick={(e) => {
                // Stop the click from bubbling to xyflow's onNodeClick
                // handler — toggling the chip strip is its own
                // affordance, NOT a "select the loop node" gesture.
                e.stopPropagation();
                setExpanded((v) => !v);
              }}
              class={[
                'text-[10px] font-semibold w-5 h-5 rounded',
                'flex items-center justify-center',
                'bg-black/5 dark:bg-white/5',
                'hover:bg-black/10 dark:hover:bg-white/10',
                'focus:outline-none focus-visible:ring-2 focus-visible:ring-amber-400',
                'motion-safe:transition-transform',
                expanded ? 'rotate-90' : '',
              ].join(' ')}
            >
              ▸
            </button>
          </div>
        </div>
        <Tooltip content={data.fullLabel ?? data.label} delay={400}>
          <span class="font-semibold text-sm truncate block w-full leading-tight">
            {data.label}
          </span>
        </Tooltip>
      </div>
      {/* Chip strip — only painted when expanded. Width is fixed by
          the outer card; overflow-x:auto handles the >20-iteration
          case so the operator can scroll laterally rather than the
          dagre layout shifting around. */}
      {expanded && iterations.length > 0 ? (
        <div
          class="flex items-center gap-1 px-3 pb-2 overflow-x-auto"
          data-testid="loop-chip-strip"
          role="list"
          aria-label={`Iteration entries for ${data.label}`}
        >
          {iterations.map((it) => (
            <button
              key={it.entry_id}
              type="button"
              role="listitem"
              data-testid="loop-chip"
              data-entry-id={it.entry_id}
              title={`Iteration ${it.iteration_n ?? '?'} (${it.status})`}
              onClick={(e) => {
                e.stopPropagation();
                onIterationClick?.(it.entry_id);
              }}
              class={[
                'shrink-0 inline-flex items-center justify-center',
                'rounded border text-[10px] font-mono leading-none',
                'h-6 min-w-[36px] px-1',
                'focus:outline-none focus-visible:ring-2 focus-visible:ring-amber-400',
                'hover:brightness-110',
                loopChipClass(it.status),
              ].join(' ')}
            >
              {it.iteration_n ?? '·'}
            </button>
          ))}
        </div>
      ) : null}
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
// LABEL_PILL_MAX_WIDTH must match the FsmEdge pill's `max-w-[280px]`
// so dagre reserves slack matching the actual rendered width.
const LABEL_PILL_MAX_WIDTH = 280;
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
  g.setGraph({
    rankdir: direction,
    nodesep: 280,
    ranksep: 120,
    edgesep: 72,
    marginx: 48,
    marginy: 48,
    ranker: nodes.length > 8 ? 'network-simplex' : 'tight-tree',
  });
  for (const n of nodes) {
    // PR 5: respect per-node width/height when the caller already
    // stamped them (loop nodes do — their card width depends on the
    // iteration count, which the run-overlay layer knows BEFORE the
    // layout pass). Fall back to the FSM defaults for every other
    // node so the existing layout numbers stay unchanged.
    const w = typeof n.width === 'number' ? n.width : NODE_WIDTH;
    const h = typeof n.height === 'number' ? n.height : NODE_HEIGHT;
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
    const w = typeof n.width === 'number' ? n.width : NODE_WIDTH;
    const h = typeof n.height === 'number' ? n.height : NODE_HEIGHT;
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
): Edge[] {
  return edges.map((e) => {
    const original = typeof e.label === 'string' ? e.label : undefined;
    const labelText = typeof e.label === 'string' ? truncateLabel(e.label) : e.label;
    // Carry the full predicate + transition metadata on edge.data so the
    // custom FsmEdge type can Tooltip it AND so onEdgeClick consumers
    // (specDetail's inspector Sheet) can render the full payload without
    // re-walking the spec.
    const incomingData = (e.data ?? {}) as Record<string, unknown>;
    const dagrePos = edgeLabelPositions?.get(e.id);
    const data: Record<string, unknown> = {
      ...incomingData,
      fullLabel: typeof incomingData.fullLabel === 'string'
        ? incomingData.fullLabel
        : original,
      sourceId: e.source,
      targetId: e.target,
      // dagreLabel propagates the layout-computed label centre to
      // FsmEdge, which prefers it over the geometric longest-segment
      // midpoint when present. This is what stops sibling labels on a
      // fan-out from stacking on the same midpoint band.
      ...(dagrePos ? { dagreLabel: dagrePos } : {}),
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

  const { positioned, edgeLabelPositions } = useMemo(() => {
    if (!autoLayout) {
      // Preserve caller-supplied positions but still tag every node
      // with the source/target Position that matches the active
      // direction. Without this, FsmNode falls back to its Right/Left
      // defaults and a TB graph would render edges attaching to the
      // wrong sides (W20 Copilot finding on #56). The custom node
      // type is also applied so callers don't have to set it manually.
      const sp = direction === 'LR' ? Position.Right : Position.Bottom;
      const tp = direction === 'LR' ? Position.Left : Position.Top;
      const manual = nodes.map((n) => {
        // PR 5: same loop dispatch as the auto-layout branch.
        const isLoop = (n.data as { isLoop?: boolean } | undefined)?.isLoop === true;
        return {
          ...n,
          // W22: stamp width/height so MiniMap renders thumbnails even
          // for callers that supply manual positions. Only fill in
          // defaults — callers that explicitly set per-node dimensions
          // (a future cardinality-aware layout) keep their values.
          width: n.width ?? NODE_WIDTH,
          height: n.height ?? NODE_HEIGHT,
          sourcePosition: n.sourcePosition ?? sp,
          targetPosition: n.targetPosition ?? tp,
          type: n.type ?? (isLoop ? 'loopNode' : 'fsmNode'),
        };
      });
      return { positioned: manual, edgeLabelPositions: new Map() as DagreEdgeLabelMap };
    }
    return applyDagreLayout(nodes, edges, direction);
  }, [nodes, edges, autoLayout, direction]);

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
        // baseline.
        const nextData = {
          ...n.data,
          isHovered,
          highlighted: isHighlighted,
          dimmed: isDimmed,
        } as FlowNodeData;
        const withSelection =
          selectedNodeId && n.id === selectedNodeId ? { ...n, selected: true } : n;
        return { ...withSelection, data: nextData };
      }),
    [positioned, selectedNodeId, hoveredNodeId, hoverActive, highlight],
  );

  const showMiniMap = miniMap ?? nodes.length > 10;

  // Compute decorated edges BEFORE any conditional return. React's
  // rules-of-hooks require every hook to be called in the same order
  // on every render; the previous version put this useMemo after the
  // empty-graph early return, which threw when the same FlowGraph
  // flipped between 0 nodes and >0 nodes (Copilot finding on PR #57).
  const decoratedEdges = useMemo(() => {
    const base = decorateEdges(edges, edgeLabelPositions);
    return base.map((e) => {
      const isHovered = hoveredEdgeId === e.id;
      const isHighlighted = !isHovered && highlight.edges.has(e.id);
      const isDimmed = hoverActive && !isHovered && !isHighlighted;
      const nextData = {
        ...(e.data ?? {}),
        isHovered,
        highlighted: isHighlighted,
        dimmed: isDimmed,
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
  }, [edges, edgeLabelPositions, hoveredEdgeId, hoverActive, highlight]);

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
        fitViewOptions={{ padding: 0.2, includeHiddenNodes: false }}
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
