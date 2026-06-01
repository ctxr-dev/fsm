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

import { useMemo } from 'preact/hooks';
import type { JSX } from 'preact';
import dagre from 'dagre';
import {
  BackgroundVariant,
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

export type FlowNodeKind = 'state' | 'worker' | 'terminal' | 'producer' | 'consumer' | 'inline';

export interface FlowNodeData extends Record<string, unknown> {
  kind: FlowNodeKind;
  label: string;
  sublabel?: string;
}

export interface FlowGraphProps {
  nodes: readonly Node<FlowNodeData>[];
  edges: readonly Edge[];
  /** When true (default), runs a dagre layered layout that OVERWRITES any
   * per-node position. Pass `false` if the caller has already assigned
   * positions (e.g. a saved manual layout) and wants them preserved. */
  autoLayout?: boolean;
  /** Layout direction. Default 'TB' (top to bottom) — natural reading for
   * FSMs, and fits a viewport-bounded panel better than LR for long chains. */
  direction?: 'LR' | 'TB';
  /** Click handler on a node (e.g. open the state-details Sheet). */
  onNodeClick?: (id: string, data: FlowNodeData) => void;
  /** Click handler on an edge. */
  onEdgeClick?: (id: string) => void;
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
}

const NODE_WIDTH = 180;
const NODE_HEIGHT = 56;

const NODE_KIND_CLASSES: Record<FlowNodeKind, string> = {
  state:
    'border-emerald-500 bg-emerald-50 dark:bg-emerald-900/30 text-emerald-900 dark:text-emerald-100',
  worker:
    'border-sky-500 bg-sky-50 dark:bg-sky-900/30 text-sky-900 dark:text-sky-100',
  inline:
    'border-violet-500 bg-violet-50 dark:bg-violet-900/30 text-violet-900 dark:text-violet-100',
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
  return (
    <div
      class={[
        'fsm-node relative rounded-md border-2 shadow-sm px-3 py-2 text-xs',
        NODE_KIND_CLASSES[kind],
        selected ? 'ring-2 ring-emerald-400 ring-offset-1' : '',
      ].join(' ')}
      style={{ width: `${NODE_WIDTH}px`, minHeight: `${NODE_HEIGHT}px` }}
    >
      <Handle
        type="target"
        position={tp}
        style={{ background: 'currentColor', width: 8, height: 8, border: 'none' }}
      />
      <div class="flex items-center justify-between gap-1">
        <span class="font-semibold truncate" title={data.label}>
          {data.label}
        </span>
        <span class="text-[10px] uppercase tracking-wide opacity-60">{kind}</span>
      </div>
      {data.sublabel ? (
        <div class="text-[10px] opacity-70 truncate" title={data.sublabel}>
          {data.sublabel}
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

const NODE_TYPES = { fsmNode: FsmNode };

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
const LABEL_WIDTH = 160;
const LABEL_HEIGHT = 18;

function applyDagreLayout(
  nodes: readonly Node<FlowNodeData>[],
  edges: readonly Edge[],
  direction: 'LR' | 'TB',
): Node<FlowNodeData>[] {
  const g = new dagre.graphlib.Graph({ multigraph: true });
  g.setDefaultEdgeLabel(() => ({}));
  g.setGraph({
    rankdir: direction,
    nodesep: direction === 'TB' ? 70 : 90,
    ranksep: direction === 'TB' ? 90 : 200,
    edgesep: 30,
    marginx: 30,
    marginy: 30,
    ranker: 'tight-tree',
  });
  for (const n of nodes) g.setNode(n.id, { width: NODE_WIDTH, height: NODE_HEIGHT });
  for (const e of edges) {
    const labelLen = typeof e.label === 'string' ? e.label.length : 0;
    // Reserve label space proportionally so dagre routes around
    // long predicate labels instead of letting them collide with
    // nodes downstream. The fourth argument is the edge name; under
    // multigraph mode it disambiguates parallel edges so a second
    // predicate between the same two states doesn't clobber the first.
    g.setEdge(
      e.source,
      e.target,
      {
        width: labelLen > 0 ? Math.min(LABEL_WIDTH, Math.max(40, labelLen * 7)) : 0,
        height: labelLen > 0 ? LABEL_HEIGHT : 0,
        labelpos: 'c',
      },
      e.id,
    );
  }
  dagre.layout(g);

  return nodes.map((n) => {
    const { x, y } = g.node(n.id);
    return {
      ...n,
      position: { x: x - NODE_WIDTH / 2, y: y - NODE_HEIGHT / 2 },
      sourcePosition: direction === 'LR' ? Position.Right : Position.Bottom,
      targetPosition: direction === 'LR' ? Position.Left : Position.Top,
      type: 'fsmNode',
    };
  });
}

// Truncate predicate strings so a single edge label can't sprawl over
// downstream nodes. Full text remains available in the tooltip
// (xyflow surfaces edge.data.fullLabel via our hover handler in a
// future iteration; today the truncated text is enough for the
// at-a-glance layout). Truncation point chosen empirically: 28 chars
// fits comfortably within the dagre-allocated LABEL_WIDTH (160 px) at
// the 10 px font-size we use.
const LABEL_MAX_CHARS = 28;
function truncateLabel(text: string): string {
  if (text.length <= LABEL_MAX_CHARS) return text;
  return text.slice(0, LABEL_MAX_CHARS - 1) + '…';
}

// Decorate edges so they render visibly in both themes: stroke via
// currentColor (the outer container sets text-color per theme), arrow
// markers so direction is unambiguous, labels with a themed fill +
// solid background rect so the text is always legible against the
// graph background.
function decorateEdges(edges: readonly Edge[]): Edge[] {
  return edges.map((e) => {
    const labelText = typeof e.label === 'string' ? truncateLabel(e.label) : e.label;
    return {
      ...e,
      // W20: default to 'step' (sharp 90-degree corners) instead of
      // 'default' (smooth bezier). For FSM graphs, orthogonal routing
      // makes the topology easier to trace at a glance — every edge
      // changes direction at the layer boundary, so the visual centre
      // line stays stable and parallel transitions stack cleanly.
      type: e.type ?? 'step',
      animated: e.animated ?? false,
      label: labelText,
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
  selectedNodeId,
  miniMap,
  controls = true,
  background = true,
  className,
}: FlowGraphProps): JSX.Element {
  const positioned = useMemo(() => {
    if (!autoLayout) {
      // Preserve caller-supplied positions but still tag every node
      // with the source/target Position that matches the active
      // direction. Without this, FsmNode falls back to its Right/Left
      // defaults and a TB graph would render edges attaching to the
      // wrong sides (W20 Copilot finding on #56). The custom node
      // type is also applied so callers don't have to set it manually.
      const sp = direction === 'LR' ? Position.Right : Position.Bottom;
      const tp = direction === 'LR' ? Position.Left : Position.Top;
      return nodes.map((n) => ({
        ...n,
        sourcePosition: n.sourcePosition ?? sp,
        targetPosition: n.targetPosition ?? tp,
        type: n.type ?? 'fsmNode',
      }));
    }
    return applyDagreLayout(nodes, edges, direction);
  }, [nodes, edges, autoLayout, direction]);

  const decoratedNodes = useMemo(
    () =>
      positioned.map((n) =>
        selectedNodeId && n.id === selectedNodeId ? { ...n, selected: true } : n,
      ),
    [positioned, selectedNodeId],
  );

  const showMiniMap = miniMap ?? nodes.length > 10;

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
  const decoratedEdges = useMemo(() => decorateEdges(edges), [edges]);
  return (
    <div
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
        fitView
        fitViewOptions={{ padding: 0.2, includeHiddenNodes: false }}
        proOptions={{ hideAttribution: true }}
        defaultEdgeOptions={{
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
        onEdgeClick={(_e, edge) => onEdgeClick?.(edge.id)}
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
    </div>
  );
}

export default FlowGraph;
