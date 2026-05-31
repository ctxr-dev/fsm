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
  ReactFlow,
  Background,
  Controls,
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
  /** When true (default), runs a dagre layered layout if nodes lack positions. */
  autoLayout?: boolean;
  /** Layout direction. Default 'LR' (left to right). */
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

function FsmNode({ data, selected }: NodeProps<Node<FlowNodeData>>): JSX.Element {
  const kind = data.kind;
  // The pixel dimensions are constants matched against the dagre
  // layout numbers; staying inline keeps the two values in literal
  // sync. Suppress the no-inline-styles lint for this single case.
  /* eslint-disable-next-line react/forbid-dom-props -- xyflow nodes need fixed pixel dimensions matched to dagre layout */
  return (
    <div
      class={[
        'fsm-node rounded-md border-2 shadow-sm px-3 py-2 text-xs',
        NODE_KIND_CLASSES[kind],
        selected ? 'ring-2 ring-emerald-400 ring-offset-1' : '',
      ].join(' ')}
      style={{ width: `${NODE_WIDTH}px`, minHeight: `${NODE_HEIGHT}px` }}
    >
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
    </div>
  );
}

const NODE_TYPES = { fsmNode: FsmNode };

/**
 * Run dagre layered layout to position nodes. Returns a fresh node
 * array with positions filled in. Idempotent — re-running on already-
 * positioned nodes produces the same result.
 */
function applyDagreLayout(
  nodes: readonly Node<FlowNodeData>[],
  edges: readonly Edge[],
  direction: 'LR' | 'TB',
): Node<FlowNodeData>[] {
  const g = new dagre.graphlib.Graph();
  g.setDefaultEdgeLabel(() => ({}));
  g.setGraph({ rankdir: direction, nodesep: 40, ranksep: 80 });
  for (const n of nodes) g.setNode(n.id, { width: NODE_WIDTH, height: NODE_HEIGHT });
  for (const e of edges) g.setEdge(e.source, e.target);
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

export function FlowGraph({
  nodes,
  edges,
  autoLayout = true,
  direction = 'LR',
  onNodeClick,
  onEdgeClick,
  selectedNodeId,
  miniMap,
  controls = true,
  background = true,
  className,
}: FlowGraphProps): JSX.Element {
  const positioned = useMemo(() => {
    if (!autoLayout) return [...nodes];
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
  return (
    <div
      class={['flow-graph relative h-full w-full min-h-[320px]', className ?? ''].join(' ')}
    >
      <ReactFlow
        nodes={decoratedNodes}
        edges={[...edges]}
        nodeTypes={NODE_TYPES}
        fitView
        proOptions={{ hideAttribution: true }}
        onNodeClick={(_e, node) => onNodeClick?.(node.id, node.data as FlowNodeData)}
        onEdgeClick={(_e, edge) => onEdgeClick?.(edge.id)}
      >
        {background ? <Background gap={16} /> : null}
        {controls ? <Controls position="bottom-right" showInteractive={false} /> : null}
        {showMiniMap ? <MiniMap pannable zoomable position="top-right" /> : null}
      </ReactFlow>
    </div>
  );
}

export default FlowGraph;
