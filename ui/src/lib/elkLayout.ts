/**
 * elkLayout — async ELK-based graph layout wrapper.
 *
 * Replaces dagre for FlowGraph's default layout pass. ELK ("Eclipse
 * Layout Kernel", via the elkjs JS port) offers two things dagre never
 * could:
 *
 *   1. First-class edge labels: every edge label is reserved as its
 *      own bounding box during routing, so labels never overlap each
 *      other or stack on the same midpoint band on a fan-out.
 *   2. ORTHOGONAL routing as a hard constraint, with proper
 *      crossing-minimisation. Dagre's `getSmoothStepPath` only made the
 *      polyline look orthogonal AFTER dagre's straight-line layout
 *      finished — labels and node corners were ignored during routing.
 *
 * Cost: ~300 KB gzipped in the main chunk (the bundled algorithm + the
 * Java->JS transpile output). User accepted that trade for the layout
 * quality win on dense skill-code-review v4 specs.
 *
 * Public surface is intentionally tiny:
 *
 *   applyElkLayout(nodes, edges, opts) -> Promise<LayoutResult>
 *
 * The function does NOT mutate its inputs; callers stamp the returned
 * positions / sections onto their own React Flow node + edge copies
 * (the existing FlowGraph helpers do exactly that, mirroring the
 * dagre code path).
 *
 * Error handling: any failure from elkjs is rethrown as ElkLayoutError
 * so FlowGraph can catch it without swallowing unrelated errors.
 */

import ELK from 'elkjs/lib/elk.bundled.js';
import type {
  ElkEdgeSection,
  ElkExtendedEdge,
  ElkLabel,
  ElkNode,
  LayoutOptions,
} from 'elkjs/lib/elk-api';
import type { Edge, Node } from '@xyflow/react';

// Default node dimensions when a caller didn't pre-stamp width/height.
// Kept in sync with FlowGraph's NODE_WIDTH / NODE_HEIGHT so the two
// layout engines reserve the same slack.
const DEFAULT_NODE_WIDTH = 270;
const DEFAULT_NODE_HEIGHT = 84;

/**
 * Singleton ELK instance. Construction is cheap (the bundle is the
 * single-file `elk.bundled.js` that already inlines every algorithm
 * the user picked into a Web Worker shim) but doing it once means a
 * graph that re-lays out on URL navigation doesn't pay the construction
 * cost more than once per tab.
 */
const elk = new ELK();

/** Per-edge routing result returned from ELK. */
export interface ElkEdgeRouting {
  /** Polyline sections returned by ELK. Sections[0] is the only one
   *  populated when the graph is single-graph (which it always is for
   *  FlowGraph today; the multi-section case applies to hierarchical
   *  graphs that we don't render). */
  sections: ElkEdgeSection[];
  /** Centre of the edge label box (graph space). Null when the edge
   *  carries no label or ELK didn't assign one. */
  labelPos: { x: number; y: number } | null;
}

/** Result of one applyElkLayout call. */
export interface LayoutResult {
  nodePositions: Map<string, { x: number; y: number }>;
  edgeRouting: Map<string, ElkEdgeRouting>;
}

/** Options accepted by applyElkLayout. */
export interface ApplyElkLayoutOptions {
  /**
   * Per-edge label dimensions (graph space). Keys are edge ids; values
   * are the rectangle ELK should reserve around the label so the routing
   * pass stays clear of it. Edges absent from this map carry no label
   * reservation (matches dagre's "no label, no slack" baseline).
   */
  labelDimensions?: Map<string, { width: number; height: number }>;
  /**
   * ELK layout options. Caller-supplied overrides win against the
   * defaults below. Exposed for tests that want to verify the option
   * block we hand to elkjs.
   */
  layoutOptions?: LayoutOptions;
}

/**
 * Error type for an ELK layout failure. FlowGraph catches this
 * specifically so it can fall back to dagre + show a one-time toast
 * without also swallowing unrelated errors from upstream code.
 */
export class ElkLayoutError extends Error {
  constructor(message: string, public readonly cause?: unknown) {
    super(message);
    this.name = 'ElkLayoutError';
  }
}

/**
 * Default ELK options. Pinned in the wrapper so every call uses the
 * same layered + orthogonal + smart-side label routing baseline.
 *
 * Tuning notes (per plan):
 *   - 'layered' + DOWN direction matches the existing TB default in
 *     FlowGraph, so the visual orientation is identical to dagre's.
 *   - edgeRouting: ORTHOGONAL is the load-bearing constraint — every
 *     polyline becomes axis-aligned by construction.
 *   - sideSelection: SMART_UP keeps all labels above the edge centreline
 *     when possible, so they line up consistently along a fan-out.
 *   - spacing.nodeNode + componentComponent + padding give the graph
 *     enough breathing room so a 15-state spec doesn't sprawl off the
 *     viewport but also doesn't pack edges through node corners.
 */
const DEFAULT_LAYOUT_OPTIONS: LayoutOptions = {
  'elk.algorithm': 'layered',
  'elk.direction': 'DOWN',
  'elk.layered.edgeRouting': 'ORTHOGONAL',
  'elk.spacing.edgeLabel': '14',
  'elk.layered.edgeLabels.sideSelection': 'SMART_UP',
  // CENTER placement is what actually makes ELK position each label
  // along the edge (the default 'HEAD'/'TAIL' modes leave x/y at 0).
  // Combined with sideSelection: SMART_UP this lands the label
  // perpendicular to the edge centreline, above it where possible.
  'elk.edgeLabels.placement': 'CENTER',
  'elk.edgeLabels.inline': 'false',
  // Visual-tune v2: shrink between-layer spacing so straight chains
  // collapse vertically (matches dagre's longest-path ranker tuning);
  // widen sibling spacing (nodeNode) so predicate pills don't stack
  // on dense fan-outs. Padding shrunk to match the dagre marginx/y
  // = 16/12 tuning so first-paint fitView leaves minimal slack.
  'elk.layered.spacing.nodeNodeBetweenLayers': '50',
  'elk.layered.spacing.edgeNodeBetweenLayers': '24',
  'elk.spacing.nodeNode': '130',
  'elk.spacing.componentComponent': '60',
  'elk.padding': '[top=12,left=16,bottom=12,right=16]',
  'elk.layered.crossingMinimization.strategy': 'LAYER_SWEEP',
  'elk.layered.layering.strategy': 'NETWORK_SIMPLEX',
};

interface NodeWithLayoutDims {
  width?: number;
  height?: number;
  data?: { layoutWidth?: number; layoutHeight?: number };
}

function nodeDimensions(node: Node): { width: number; height: number } {
  // PR 5 (loop nodes) and future cardinality-aware callers stamp
  // explicit width/height on the React Flow node before layout. Honour
  // those; fall back to the default card dimensions otherwise.
  const n = node as NodeWithLayoutDims;
  const fromData = n.data;
  const w =
    typeof n.width === 'number'
      ? n.width
      : typeof fromData?.layoutWidth === 'number'
      ? fromData.layoutWidth
      : DEFAULT_NODE_WIDTH;
  const h =
    typeof n.height === 'number'
      ? n.height
      : typeof fromData?.layoutHeight === 'number'
      ? fromData.layoutHeight
      : DEFAULT_NODE_HEIGHT;
  return { width: w, height: h };
}

/**
 * Apply ELK layout to a graph and return per-node positions + per-edge
 * routing + per-edge label positions.
 *
 * @param nodes - React Flow nodes (intrinsic dimensions read from
 *                node.width/height OR data.layoutWidth/layoutHeight).
 * @param edges - React Flow edges. ids must be globally unique;
 *                specGraph already enforces this for parallel edges.
 * @param opts  - Per-edge label dimensions + optional layout-options
 *                overrides.
 *
 * @returns Promise<LayoutResult> resolving to maps from id to position
 *          / routing. The returned maps are fresh on every call (safe
 *          to mutate without affecting subsequent layouts).
 *
 * @throws ElkLayoutError if elkjs throws or returns an unexpected shape.
 */
export async function applyElkLayout(
  nodes: readonly Node[],
  edges: readonly Edge[],
  opts: ApplyElkLayoutOptions = {},
): Promise<LayoutResult> {
  const labelDimensions = opts.labelDimensions ?? new Map();
  const layoutOptions = { ...DEFAULT_LAYOUT_OPTIONS, ...(opts.layoutOptions ?? {}) };

  // Build the ELK input graph. Each React Flow node becomes an ElkNode
  // child of the synthetic root; each edge becomes an ElkExtendedEdge
  // with sources/targets (single-element arrays — ELK supports
  // hyperedges via N>1, but FlowGraph never produces them).
  const elkNodes: ElkNode[] = nodes.map((n) => {
    const { width, height } = nodeDimensions(n);
    return { id: n.id, width, height };
  });

  const elkEdges: ElkExtendedEdge[] = edges.map((e) => {
    const dim = labelDimensions.get(e.id);
    // Non-empty text is required for ELK to actually run the label
    // placement pass (empty text strings are treated as "no label" and
    // leave the label rect at the origin). A single space carries no
    // visual meaning here; it just unblocks placement.
    const labels: ElkLabel[] = dim
      ? [{ text: ' ', width: dim.width, height: dim.height }]
      : [];
    return {
      id: e.id,
      sources: [e.source],
      targets: [e.target],
      labels,
    };
  });

  const rootGraph: ElkNode = {
    id: 'root',
    layoutOptions,
    children: elkNodes,
    edges: elkEdges,
  };

  let laidOut: ElkNode;
  try {
    laidOut = await elk.layout(rootGraph);
  } catch (err) {
    throw new ElkLayoutError('ELK layout failed', err);
  }

  const nodePositions = new Map<string, { x: number; y: number }>();
  for (const child of laidOut.children ?? []) {
    // ELK returns x/y at the top-left of the node bounding box, which
    // is exactly what React Flow expects as node.position (the dagre
    // path centred-then-offset; ELK lets us skip that arithmetic).
    if (typeof child.x === 'number' && typeof child.y === 'number') {
      nodePositions.set(child.id, { x: child.x, y: child.y });
    }
  }

  const edgeRouting = new Map<string, ElkEdgeRouting>();
  for (const edge of (laidOut.edges ?? []) as ElkExtendedEdge[]) {
    const sections = edge.sections ?? [];
    // ELK places label rectangles via the same labels[] array we passed
    // in; the resulting label carries x/y at its top-left. Convert to
    // centre coords here (FsmEdge's EdgeLabelRenderer translates to
    // `-50%, -50%` relative to the centre).
    const labelObj = (edge.labels ?? [])[0];
    let labelPos: { x: number; y: number } | null = null;
    if (
      labelObj &&
      typeof labelObj.x === 'number' &&
      typeof labelObj.y === 'number'
    ) {
      const w = typeof labelObj.width === 'number' ? labelObj.width : 0;
      const h = typeof labelObj.height === 'number' ? labelObj.height : 0;
      labelPos = { x: labelObj.x + w / 2, y: labelObj.y + h / 2 };
    }
    edgeRouting.set(edge.id, { sections, labelPos });
  }

  return { nodePositions, edgeRouting };
}

export type { ElkEdgeSection };
