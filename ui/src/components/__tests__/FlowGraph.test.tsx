/**
 * Tests for components/FlowGraph.tsx.
 *
 * @xyflow/react requires a real browser layout engine (DOMRect,
 * ResizeObserver, fully-fledged SVG) that jsdom cannot fully
 * emulate. We mock the library here so the unit tests can verify
 * what FlowGraph itself owns: the empty-state fallback, the dagre
 * layout side-effect (positions get filled in), and the node-kind
 * variant prop pass-through. The library's actual render is
 * exercised in the e2e battery (W18k) where a real Chromium runs.
 */

import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { act, cleanup, render } from '@testing-library/preact';

// Mock @xyflow/react before importing FlowGraph.
let lastReactFlowProps: Record<string, unknown> | null = null;
vi.mock('@xyflow/react', () => ({
  ReactFlow: (props: Record<string, unknown> & { children?: unknown }) => {
    lastReactFlowProps = props;
    return <div data-testid="mock-react-flow">{props.children as any}</div>;
  },
  Background: () => <div data-testid="bg" />,
  Controls: () => <div data-testid="controls" />,
  MiniMap: () => <div data-testid="minimap" />,
  Handle: () => null,
  BaseEdge: () => null,
  EdgeLabelRenderer: (p: { children?: unknown }) => <>{p.children as any}</>,
  getSmoothStepPath: () => ['M0,0 L1,1', 0, 0, 0, 0],
  Position: { Left: 'left', Right: 'right', Top: 'top', Bottom: 'bottom' },
  BackgroundVariant: { Dots: 'dots', Lines: 'lines', Cross: 'cross' },
  MarkerType: { ArrowClosed: 'arrowclosed', Arrow: 'arrow' },
  PanOnScrollMode: { Free: 'free', Vertical: 'vertical', Horizontal: 'horizontal' },
}));
vi.mock('@xyflow/react/dist/style.css', () => ({}));

import { FlowGraph, type FlowNodeData } from '../FlowGraph';
import type { Edge, Node } from '@xyflow/react';

beforeEach(() => {
  lastReactFlowProps = null;
});
afterEach(() => cleanup());

describe('FlowGraph', () => {
  test('empty graph renders the "No graph data" fallback (skips ReactFlow)', () => {
    const { getByText, queryByTestId } = render(<FlowGraph nodes={[]} edges={[]} />);
    expect(getByText('No graph data')).toBeInTheDocument();
    expect(queryByTestId('mock-react-flow')).toBeNull();
  });

  test('renders ReactFlow when nodes present', () => {
    const nodes: Node<FlowNodeData>[] = [
      { id: '1', position: { x: 0, y: 0 }, data: { kind: 'state', label: 'plan' } },
    ];
    const { getByTestId } = render(<FlowGraph nodes={nodes} edges={[]} />);
    expect(getByTestId('mock-react-flow')).toBeInTheDocument();
  });

  test('dagre auto-layout fills in positions for unpositioned nodes', () => {
    const nodes: Node<FlowNodeData>[] = [
      { id: 'a', position: { x: 0, y: 0 }, data: { kind: 'worker', label: 'a' } },
      { id: 'b', position: { x: 0, y: 0 }, data: { kind: 'state', label: 'b' } },
      { id: 'c', position: { x: 0, y: 0 }, data: { kind: 'terminal', label: 'c' } },
    ];
    const edges: Edge[] = [
      { id: 'a-b', source: 'a', target: 'b' },
      { id: 'b-c', source: 'b', target: 'c' },
    ];
    render(<FlowGraph nodes={nodes} edges={edges} autoLayout={true} direction="LR" />);
    const positioned = lastReactFlowProps!.nodes as Node<FlowNodeData>[];
    // Dagre lays out in a chain; positions must differ across all three nodes.
    const xs = positioned.map((n) => n.position.x);
    const uniqueX = new Set(xs);
    expect(uniqueX.size).toBe(3);
    // All nodes should be tagged with our custom node type.
    expect(positioned.every((n) => n.type === 'fsmNode')).toBe(true);
  });

  test('autoLayout=false preserves caller positions but stamps direction-aware handle sides', () => {
    const nodes: Node<FlowNodeData>[] = [
      { id: 'a', position: { x: 100, y: 200 }, data: { kind: 'state', label: 'a' } },
    ];
    render(<FlowGraph nodes={nodes} edges={[]} autoLayout={false} direction="TB" />);
    const passedTB = lastReactFlowProps!.nodes as Array<
      Node<FlowNodeData> & { sourcePosition?: string; targetPosition?: string; type?: string }
    >;
    expect(passedTB[0].position).toEqual({ x: 100, y: 200 });
    // TB: edges attach to bottom of source, top of target.
    expect(passedTB[0].sourcePosition).toBe('bottom');
    expect(passedTB[0].targetPosition).toBe('top');
    expect(passedTB[0].type).toBe('fsmNode');

    // Same nodes but LR direction → handles flip to right/left.
    render(<FlowGraph nodes={nodes} edges={[]} autoLayout={false} direction="LR" />);
    const passedLR = lastReactFlowProps!.nodes as Array<
      Node<FlowNodeData> & { sourcePosition?: string; targetPosition?: string }
    >;
    expect(passedLR[0].sourcePosition).toBe('right');
    expect(passedLR[0].targetPosition).toBe('left');
  });

  test('selectedNodeId decorates the matching node with selected=true', () => {
    const nodes: Node<FlowNodeData>[] = [
      { id: 'a', position: { x: 0, y: 0 }, data: { kind: 'state', label: 'a' } },
      { id: 'b', position: { x: 0, y: 0 }, data: { kind: 'state', label: 'b' } },
    ];
    render(<FlowGraph nodes={nodes} edges={[]} selectedNodeId="b" autoLayout={false} />);
    const decorated = lastReactFlowProps!.nodes as (Node<FlowNodeData> & { selected?: boolean })[];
    expect(decorated.find((n) => n.id === 'a')?.selected).toBeUndefined();
    expect(decorated.find((n) => n.id === 'b')?.selected).toBe(true);
  });

  test('miniMap defaults: hidden for <=10 nodes, shown for >10', () => {
    const small: Node<FlowNodeData>[] = [
      { id: 'a', position: { x: 0, y: 0 }, data: { kind: 'state', label: 'a' } },
    ];
    const { queryByTestId, rerender } = render(<FlowGraph nodes={small} edges={[]} />);
    expect(queryByTestId('minimap')).toBeNull();
    const big = Array.from({ length: 15 }, (_, i) => ({
      id: `n${i}`,
      position: { x: 0, y: 0 },
      data: { kind: 'state' as const, label: `n${i}` },
    }));
    rerender(<FlowGraph nodes={big} edges={[]} />);
    expect(queryByTestId('minimap')).not.toBeNull();
  });

  test('controls + background can be disabled', () => {
    const nodes: Node<FlowNodeData>[] = [
      { id: 'a', position: { x: 0, y: 0 }, data: { kind: 'state', label: 'a' } },
    ];
    const { queryByTestId } = render(
      <FlowGraph nodes={nodes} edges={[]} controls={false} background={false} />,
    );
    expect(queryByTestId('bg')).toBeNull();
    expect(queryByTestId('controls')).toBeNull();
  });

  test('W23b: decorateEdges preserves full label text (FsmEdge handles wrap + tooltip)', () => {
    // Pre-W23b, FlowGraph pre-truncated visible edge labels to 28
    // chars with an ellipsis. The user flagged the truncation as a
    // regression (long predicates rendered as "tier == 'trivial' AND
    // l..." with no way to read the rest without opening the
    // inspector). The fix moves visual presentation responsibility to
    // FsmEdge: a 280px max-width pill with break-words wrapping, plus
    // a hover Tooltip for the full text. decorateEdges therefore now
    // passes the original label through unchanged.
    const nodes: Node<FlowNodeData>[] = [
      { id: 'a', position: { x: 0, y: 0 }, data: { kind: 'state', label: 'a' } },
      { id: 'b', position: { x: 0, y: 0 }, data: { kind: 'state', label: 'b' } },
    ];
    const longLabel =
      "tier == 'trivial' AND len(risk_signals) == 0 AND NOT scope_overrides_present";
    const edges: Edge[] = [{ id: 'a-b', source: 'a', target: 'b', label: longLabel }];
    render(<FlowGraph nodes={nodes} edges={edges} autoLayout={false} />);
    const decorated = lastReactFlowProps!.edges as Edge[];
    const lbl = decorated[0].label as string;
    expect(lbl).toBe(longLabel);
    // Confirm the legacy ellipsis suffix (U+2026) is no longer applied.
    expect(lbl.endsWith('…')).toBe(false);
  });

  test('decorateEdges leaves short labels untouched', () => {
    const nodes: Node<FlowNodeData>[] = [
      { id: 'a', position: { x: 0, y: 0 }, data: { kind: 'state', label: 'a' } },
      { id: 'b', position: { x: 0, y: 0 }, data: { kind: 'state', label: 'b' } },
    ];
    const edges: Edge[] = [{ id: 'a-b', source: 'a', target: 'b', label: 'always' }];
    render(<FlowGraph nodes={nodes} edges={edges} autoLayout={false} />);
    const decorated = lastReactFlowProps!.edges as Edge[];
    expect(decorated[0].label).toBe('always');
  });

  test('decorateEdges enables themed label background badge', () => {
    const nodes: Node<FlowNodeData>[] = [
      { id: 'a', position: { x: 0, y: 0 }, data: { kind: 'state', label: 'a' } },
      { id: 'b', position: { x: 0, y: 0 }, data: { kind: 'state', label: 'b' } },
    ];
    const edges: Edge[] = [{ id: 'a-b', source: 'a', target: 'b', label: 'always' }];
    render(<FlowGraph nodes={nodes} edges={edges} autoLayout={false} />);
    const decorated = lastReactFlowProps!.edges as Array<
      Edge & {
        labelShowBg?: boolean;
        labelBgStyle?: { fill?: string };
      }
    >;
    expect(decorated[0].labelShowBg).toBe(true);
    expect(decorated[0].labelBgStyle?.fill).toContain('--xy-label-bg');
  });

  test('parallel edges between the same nodes are added to dagre as a multigraph', async () => {
    // specToGraph emits two transitions between the same (source, target):
    // a deterministic predicate and an `otherwise` fallback. A non-
    // multigraph dagre Graph would collapse them under setEdge(v, w, ...)
    // and lose one label's layout slack. To verify the FIX (not just
    // that React Flow forwards both edges, which it always would), we
    // wrap dagre.graphlib.Graph so we can inspect the actual Graph
    // instance FlowGraph builds: constructor option, edge count,
    // per-edge names.
    const dagreMod = await import('dagre');
    // dagre.graphlib.Graph is a class; we treat it as an unknown
    // constructor here so we can wrap it without fighting TS over
    // its overloaded signature.
    type GraphCtor = new (opts?: Record<string, unknown>) => unknown;
    const graphlib = dagreMod.default.graphlib as unknown as { Graph: GraphCtor };
    const realGraph = graphlib.Graph;
    const instances: unknown[] = [];
    const ctorCalls: unknown[] = [];
    // Replace with a wrapper that constructs a real Graph and records
    // both the args and the instance. Using `function` (not arrow) so
    // `new`-call semantics work; the inner `new realGraph(opts)` still
    // gives us the genuine dagre behaviour for the rest of the layout.
    const Wrapped = function (opts?: Record<string, unknown>) {
      ctorCalls.push(opts);
      const inst = new realGraph(opts);
      instances.push(inst);
      return inst;
    } as unknown as GraphCtor;
    graphlib.Graph = Wrapped;
    try {
      const nodes: Node<FlowNodeData>[] = [
        { id: 'a', position: { x: 0, y: 0 }, data: { kind: 'state', label: 'a' } },
        { id: 'b', position: { x: 0, y: 0 }, data: { kind: 'state', label: 'b' } },
      ];
      const edges: Edge[] = [
        { id: 'a-b-0', source: 'a', target: 'b', label: 'predicate' },
        { id: 'a-b-1', source: 'a', target: 'b', label: 'otherwise' },
      ];
      render(<FlowGraph nodes={nodes} edges={edges} autoLayout={true} direction="TB" />);

      // 1. Graph constructor was called with multigraph: true.
      expect(ctorCalls).toEqual([{ multigraph: true }]);

      // 2. The live Graph instance built by FlowGraph has BOTH edges as
      //    distinct dagre entries (edgeCount() reflects multigraph state).
      expect(instances).toHaveLength(1);
      const instance = instances[0] as unknown as {
        edgeCount: () => number;
        edges: () => Array<{ v: string; w: string; name?: string }>;
      };
      expect(instance.edgeCount()).toBe(2);
      const dagreEdges = instance.edges();
      // 3. Each parallel edge carries a distinct name (the React Flow edge id),
      //    which is what makes multigraph mode actually disambiguate them.
      const names = dagreEdges.map((e) => e.name).sort();
      expect(names).toEqual(['a-b-0', 'a-b-1']);
    } finally {
      graphlib.Graph = realGraph;
    }
  });

  test('W21: decorateEdges sets type=fsmEdge and copies full label onto edge.data', () => {
    const nodes: Node<FlowNodeData>[] = [
      { id: 'a', position: { x: 0, y: 0 }, data: { kind: 'state', label: 'a' } },
      { id: 'b', position: { x: 0, y: 0 }, data: { kind: 'state', label: 'b' } },
    ];
    const fullPredicate = "tier == 'trivial' AND len(risk_signals) == 0 AND NOT scope_overrides_present";
    const edges: Edge[] = [{ id: 'a-b', source: 'a', target: 'b', label: fullPredicate }];
    render(<FlowGraph nodes={nodes} edges={edges} autoLayout={false} />);
    const decorated = lastReactFlowProps!.edges as Array<Edge & { type?: string; data?: Record<string, unknown> }>;
    expect(decorated[0].type).toBe('fsmEdge');
    expect(decorated[0].data?.fullLabel).toBe(fullPredicate);
    expect(decorated[0].data?.sourceId).toBe('a');
    expect(decorated[0].data?.targetId).toBe('b');
    // W23b: full text is preserved on BOTH the visible label and
    // data.fullLabel; FsmEdge handles wrap + tooltip.
    expect(decorated[0].label).toBe(fullPredicate);
  });

  test('W21: FlowGraph registers fsmEdge as a custom edge type', () => {
    const nodes: Node<FlowNodeData>[] = [
      { id: 'a', position: { x: 0, y: 0 }, data: { kind: 'state', label: 'a' } },
    ];
    render(<FlowGraph nodes={nodes} edges={[]} />);
    const edgeTypes = lastReactFlowProps!.edgeTypes as Record<string, unknown>;
    expect(edgeTypes).toBeDefined();
    expect(edgeTypes.fsmEdge).toBeTypeOf('function');
  });

  test('W21: FlowGraph dispatches edge clicks via Preact Context (not a module registry)', async () => {
    // Render FsmEdge directly within a FlowGraph; the FsmEdge label
    // click handler should read its dispatch target from the Context
    // provider FlowGraph wraps around its subtree, not from any
    // module-level singleton (which would leak across instances).
    const { FsmEdgeClickContext, FsmEdge } = await import('../FsmEdge');
    const handler = vi.fn();
    const { getByText } = render(
      <FsmEdgeClickContext.Provider value={handler}>
        <FsmEdge
          {...({
            id: 'a-b',
            sourceX: 0,
            sourceY: 0,
            targetX: 10,
            targetY: 10,
            sourcePosition: 'right',
            targetPosition: 'left',
            label: 'always',
            data: { fullLabel: 'always', sourceId: 'a', targetId: 'b' },
          } as unknown as Parameters<typeof FsmEdge>[0])}
        />
      </FsmEdgeClickContext.Provider>,
    );
    const label = getByText('always') as HTMLButtonElement;
    label.click();
    expect(handler).toHaveBeenCalledWith('a-b', expect.objectContaining({
      fullLabel: 'always',
      sourceId: 'a',
      targetId: 'b',
    }));
  });

  test('W21: onEdgeClick receives edge.data as second arg', () => {
    const nodes: Node<FlowNodeData>[] = [
      { id: 'a', position: { x: 0, y: 0 }, data: { kind: 'state', label: 'a' } },
      { id: 'b', position: { x: 0, y: 0 }, data: { kind: 'state', label: 'b' } },
    ];
    const edges: Edge[] = [{ id: 'a-b', source: 'a', target: 'b', label: 'always' }];
    const onEdgeClick = vi.fn();
    render(<FlowGraph nodes={nodes} edges={edges} onEdgeClick={onEdgeClick} autoLayout={false} />);
    // Pull the onEdgeClick out of the forwarded ReactFlow props and
    // invoke it with a synthetic (event, edge) pair (mirrors how
    // xyflow would dispatch on a real click).
    const rfOnEdgeClick = lastReactFlowProps!.onEdgeClick as (e: unknown, edge: Edge) => void;
    rfOnEdgeClick({}, { id: 'a-b', source: 'a', target: 'b', data: { fullLabel: 'always', sourceId: 'a', targetId: 'b' } } as unknown as Edge);
    expect(onEdgeClick).toHaveBeenCalledWith('a-b', expect.objectContaining({
      fullLabel: 'always',
      sourceId: 'a',
      targetId: 'b',
    }));
  });

  describe('W23d 1-hop hover highlight', () => {
    // Helper to build a small graph: a -> b -> c with a side branch d
    // attached to b. This gives us a node (b) with TWO neighbours
    // (a + c) plus one unrelated node (d when hovering a, or d only
    // when nothing else is hovered). The graph stays small enough to
    // assert exact sets without hand-counting.
    const buildGraph = (): {
      nodes: Node<FlowNodeData>[];
      edges: Edge[];
    } => ({
      nodes: [
        { id: 'a', position: { x: 0, y: 0 }, data: { kind: 'state', label: 'a' } },
        { id: 'b', position: { x: 0, y: 0 }, data: { kind: 'state', label: 'b' } },
        { id: 'c', position: { x: 0, y: 0 }, data: { kind: 'state', label: 'c' } },
        { id: 'd', position: { x: 0, y: 0 }, data: { kind: 'state', label: 'd' } },
      ],
      edges: [
        { id: 'a-b', source: 'a', target: 'b', label: 'first' },
        { id: 'b-c', source: 'b', target: 'c', label: 'next' },
        { id: 'a-d', source: 'a', target: 'd', label: 'side' },
      ],
    });

    test('no hover: every node + edge renders un-dimmed and un-highlighted', () => {
      const { nodes, edges } = buildGraph();
      render(<FlowGraph nodes={nodes} edges={edges} autoLayout={false} />);
      const decoratedNodes = lastReactFlowProps!.nodes as Node<FlowNodeData>[];
      const decoratedEdges = lastReactFlowProps!.edges as Array<
        Edge & { data?: { dimmed?: boolean; highlighted?: boolean; isHovered?: boolean } }
      >;
      // Baseline: nothing dimmed, nothing highlighted, nothing hovered.
      for (const n of decoratedNodes) {
        expect(n.data.dimmed).toBe(false);
        expect(n.data.highlighted).toBe(false);
        expect(n.data.isHovered).toBe(false);
      }
      for (const e of decoratedEdges) {
        expect(e.data?.dimmed).toBe(false);
        expect(e.data?.highlighted).toBe(false);
        expect(e.data?.isHovered).toBe(false);
      }
    });

    test('hovering node "a" highlights a + its neighbours (b, d) + the connecting edges; dims c + b-c', () => {
      const { nodes, edges } = buildGraph();
      render(<FlowGraph nodes={nodes} edges={edges} autoLayout={false} />);
      const onNodeMouseEnter = lastReactFlowProps!.onNodeMouseEnter as (
        e: unknown,
        node: { id: string },
      ) => void;
      act(() => {
        onNodeMouseEnter({}, { id: 'a' });
      });
      const decoratedNodes = lastReactFlowProps!.nodes as Node<FlowNodeData>[];
      const decoratedEdges = lastReactFlowProps!.edges as Array<
        Edge & { data?: { dimmed?: boolean; highlighted?: boolean; isHovered?: boolean } }
      >;
      const byId = Object.fromEntries(decoratedNodes.map((n) => [n.id, n]));
      // a is THE hover target.
      expect(byId.a.data.isHovered).toBe(true);
      expect(byId.a.data.dimmed).toBe(false);
      // b and d are 1-hop neighbours of a (via a-b and a-d).
      expect(byId.b.data.highlighted).toBe(true);
      expect(byId.b.data.dimmed).toBe(false);
      expect(byId.d.data.highlighted).toBe(true);
      expect(byId.d.data.dimmed).toBe(false);
      // c is 2 hops away — must be dimmed.
      expect(byId.c.data.highlighted).toBe(false);
      expect(byId.c.data.dimmed).toBe(true);

      const edgesById = Object.fromEntries(decoratedEdges.map((e) => [e.id, e]));
      expect(edgesById['a-b'].data?.highlighted).toBe(true);
      expect(edgesById['a-b'].data?.dimmed).toBe(false);
      expect(edgesById['a-d'].data?.highlighted).toBe(true);
      expect(edgesById['a-d'].data?.dimmed).toBe(false);
      expect(edgesById['b-c'].data?.highlighted).toBe(false);
      expect(edgesById['b-c'].data?.dimmed).toBe(true);
    });

    test('hovering edge "a-b" highlights the edge + its endpoints (a, b); dims c, d and the other edges', () => {
      const { nodes, edges } = buildGraph();
      render(<FlowGraph nodes={nodes} edges={edges} autoLayout={false} />);
      const onEdgeMouseEnter = lastReactFlowProps!.onEdgeMouseEnter as (
        e: unknown,
        edge: { id: string },
      ) => void;
      act(() => {
        onEdgeMouseEnter({}, { id: 'a-b' });
      });
      const decoratedNodes = lastReactFlowProps!.nodes as Node<FlowNodeData>[];
      const decoratedEdges = lastReactFlowProps!.edges as Array<
        Edge & {
          data?: { dimmed?: boolean; highlighted?: boolean; isHovered?: boolean };
          style?: { strokeWidth?: number; opacity?: number };
        }
      >;
      const nodeById = Object.fromEntries(decoratedNodes.map((n) => [n.id, n]));
      // Endpoints of a-b stay full opacity and pick up the subtle ring
      // signal via data.highlighted.
      expect(nodeById.a.data.highlighted).toBe(true);
      expect(nodeById.a.data.dimmed).toBe(false);
      expect(nodeById.b.data.highlighted).toBe(true);
      expect(nodeById.b.data.dimmed).toBe(false);
      // c and d are not endpoints of a-b -> dimmed.
      expect(nodeById.c.data.dimmed).toBe(true);
      expect(nodeById.d.data.dimmed).toBe(true);

      const edgeById = Object.fromEntries(decoratedEdges.map((e) => [e.id, e]));
      expect(edgeById['a-b'].data?.isHovered).toBe(true);
      expect(edgeById['a-b'].data?.dimmed).toBe(false);
      // Hovered edge gets a thicker stroke.
      expect(edgeById['a-b'].style?.strokeWidth).toBeGreaterThan(1.5);
      expect(edgeById['a-b'].style?.opacity).toBe(1);
      // Other edges fade and stay at baseline stroke.
      expect(edgeById['a-d'].data?.dimmed).toBe(true);
      expect(edgeById['a-d'].style?.opacity).toBe(0.3);
      expect(edgeById['b-c'].data?.dimmed).toBe(true);
      expect(edgeById['b-c'].style?.opacity).toBe(0.3);
    });

    test('node hover then leave returns to the un-hovered baseline', () => {
      const { nodes, edges } = buildGraph();
      render(<FlowGraph nodes={nodes} edges={edges} autoLayout={false} />);
      const onNodeMouseEnter = lastReactFlowProps!.onNodeMouseEnter as (
        e: unknown,
        node: { id: string },
      ) => void;
      const onNodeMouseLeave = lastReactFlowProps!.onNodeMouseLeave as (
        e: unknown,
        node: { id: string },
      ) => void;
      act(() => {
        onNodeMouseEnter({}, { id: 'a' });
      });
      // Confirm at least one node is dimmed mid-hover, then leave and
      // confirm nothing is dimmed.
      let decoratedNodes = lastReactFlowProps!.nodes as Node<FlowNodeData>[];
      expect(decoratedNodes.some((n) => n.data.dimmed === true)).toBe(true);
      act(() => {
        onNodeMouseLeave({}, { id: 'a' });
      });
      decoratedNodes = lastReactFlowProps!.nodes as Node<FlowNodeData>[];
      expect(decoratedNodes.every((n) => n.data.dimmed === false)).toBe(true);
      expect(decoratedNodes.every((n) => n.data.highlighted === false)).toBe(true);
      expect(decoratedNodes.every((n) => n.data.isHovered === false)).toBe(true);
    });

    test('entering an edge clears any active node hover (mutually exclusive)', () => {
      const { nodes, edges } = buildGraph();
      render(<FlowGraph nodes={nodes} edges={edges} autoLayout={false} />);
      const onNodeMouseEnter = lastReactFlowProps!.onNodeMouseEnter as (
        e: unknown,
        node: { id: string },
      ) => void;
      const onEdgeMouseEnter = lastReactFlowProps!.onEdgeMouseEnter as (
        e: unknown,
        edge: { id: string },
      ) => void;
      act(() => {
        onNodeMouseEnter({}, { id: 'a' });
      });
      act(() => {
        onEdgeMouseEnter({}, { id: 'b-c' });
      });
      const decoratedNodes = lastReactFlowProps!.nodes as Node<FlowNodeData>[];
      const byId = Object.fromEntries(decoratedNodes.map((n) => [n.id, n]));
      // Node hover should be cleared; only b + c (endpoints of b-c)
      // are highlighted now.
      expect(byId.a.data.isHovered).toBe(false);
      expect(byId.a.data.highlighted).toBe(false);
      expect(byId.a.data.dimmed).toBe(true);
      expect(byId.b.data.highlighted).toBe(true);
      expect(byId.c.data.highlighted).toBe(true);
      expect(byId.d.data.dimmed).toBe(true);
    });
  });

  describe('PR 5: loop node + iteration chip strip', () => {
    function loopNode(
      id: string,
      iterations: Array<{ entry_id: string; iteration_n: number | null; status: string }>,
      maxIterations = iterations.length,
    ): Node<FlowNodeData> {
      return {
        id,
        position: { x: 0, y: 0 },
        data: {
          kind: 'loop',
          label: id,
          isLoop: true,
          loopMaxIterations: maxIterations,
          iterationCount: iterations.length,
          iterationEntries: iterations,
        } as FlowNodeData,
      };
    }

    test('loop nodes get type=loopNode and loopNode is registered with ReactFlow', () => {
      const nodes = [loopNode('l', [{ entry_id: 'e1', iteration_n: 1, status: 'exited' }])];
      render(<FlowGraph nodes={nodes} edges={[]} autoLayout={false} />);
      const passed = lastReactFlowProps!.nodes as Array<Node<FlowNodeData> & { type?: string }>;
      expect(passed[0].type).toBe('loopNode');
      const nodeTypes = lastReactFlowProps!.nodeTypes as Record<string, unknown>;
      expect(nodeTypes.loopNode).toBeTypeOf('function');
      expect(nodeTypes.fsmNode).toBeTypeOf('function');
    });

    test('non-loop nodes keep type=fsmNode (dispatch discrimination)', () => {
      const nodes: Node<FlowNodeData>[] = [
        { id: 'w', position: { x: 0, y: 0 }, data: { kind: 'worker', label: 'w' } },
      ];
      render(<FlowGraph nodes={nodes} edges={[]} autoLayout={false} />);
      const passed = lastReactFlowProps!.nodes as Array<Node<FlowNodeData> & { type?: string }>;
      expect(passed[0].type).toBe('fsmNode');
    });

    test('loop node is collapsed by default; expanding renders the chip strip; chip click fires onIterationClick', async () => {
      // Mount the loopNode renderer via the LoopIterationClickContext
      // bridge — same pattern the FsmEdge test uses for FsmEdgeClickContext.
      // The renderer is a Preact component (uses hooks), so it must be
      // mounted via JSX inside render(), not invoked as a function.
      const fg = await import('../FlowGraph');
      const LoopIterationClickContext = (fg as unknown as {
        LoopIterationClickContext: import('preact').Context<((id: string) => void) | null>;
      }).LoopIterationClickContext;
      const handler = vi.fn();
      const iterations = [
        { entry_id: 'e1', iteration_n: 1, status: 'exited' },
        { entry_id: 'e2', iteration_n: 2, status: 'entered' },
      ];
      const data = {
        kind: 'loop',
        label: 'tick',
        isLoop: true,
        loopMaxIterations: 5,
        iterationCount: 2,
        iterationEntries: iterations,
      } as FlowNodeData;

      // Pull the registered loopNode component out of FlowGraph's
      // NODE_TYPES map via the captured ReactFlow props. That way we
      // test the ACTUAL renderer FlowGraph wires up, not a copy.
      render(<FlowGraph nodes={[loopNode('tick', iterations, 5)]} edges={[]} autoLayout={false} />);
      const nodeTypes = lastReactFlowProps!.nodeTypes as Record<string, any>;
      const LoopComponent = nodeTypes.loopNode as (props: {
        data: FlowNodeData;
        selected?: boolean;
      }) => any;

      const { getByTestId, queryByTestId, getAllByTestId } = render(
        <LoopIterationClickContext.Provider value={handler}>
          <LoopComponent data={data} />
        </LoopIterationClickContext.Provider>,
      );

      // Collapsed by default: no chip strip yet.
      expect(queryByTestId('loop-chip-strip')).toBeNull();
      // Header has the ×N badge.
      expect(getByTestId('loop-iteration-badge').textContent).toContain('2');
      // Toggle to expand.
      const toggle = getByTestId('loop-expand-toggle') as HTMLButtonElement;
      act(() => {
        toggle.click();
      });
      // Chip strip now visible with one chip per iteration.
      const strip = getByTestId('loop-chip-strip');
      expect(strip).not.toBeNull();
      const chips = getAllByTestId('loop-chip');
      expect(chips).toHaveLength(2);
      expect(chips[0].getAttribute('data-entry-id')).toBe('e1');
      expect(chips[1].getAttribute('data-entry-id')).toBe('e2');
      // Clicking a chip fires onIterationClick with the entry_id.
      act(() => {
        (chips[1] as HTMLButtonElement).click();
      });
      expect(handler).toHaveBeenCalledWith('e2');
      // Toggle back to collapsed.
      act(() => {
        toggle.click();
      });
      expect(queryByTestId('loop-chip-strip')).toBeNull();
    });

    test('PR 5: layout width is bounded by min(20, iterations) * 40 + header for 0 / 1 / 5 / 200 iterations', () => {
      // Use the auto-layout branch so dagre is actually exercised — the
      // pre-stamped n.width must flow through to the positioned node.
      const headerWidth = 270;
      const chipWidth = 40;
      const max = 20;
      const cases = [0, 1, 5, 200];
      for (const count of cases) {
        const iter = Array.from({ length: count }, (_, i) => ({
          entry_id: `e${i}`,
          iteration_n: i + 1,
          status: 'exited',
        }));
        // Pre-stamp width on the node the same way specGraph/runGraph
        // does, so dagre sees the right dimensions.
        const expanded = headerWidth + Math.min(max, count) * chipWidth;
        const nodes: Node<FlowNodeData>[] = [
          { ...loopNode('l', iter, count), width: expanded, height: 120 },
        ];
        render(<FlowGraph nodes={nodes} edges={[]} autoLayout={true} />);
        const passed = lastReactFlowProps!.nodes as Array<Node<FlowNodeData> & { width?: number }>;
        // The positioned node keeps the input width unchanged for loop
        // nodes — non-default widths flow through dagre + back out.
        expect(passed[0].width).toBe(expanded);
        // The cap at 20 is the load-bearing assertion: 200 iterations
        // must NOT produce 8000+ px.
        expect(passed[0].width).toBeLessThanOrEqual(headerWidth + max * chipWidth);
      }
    });

    test('PR 5: a loop with 50 iterations renders width bounded by min(20,50)*40 + header', () => {
      const headerWidth = 270;
      const chipWidth = 40;
      const max = 20;
      const iter = Array.from({ length: 50 }, (_, i) => ({
        entry_id: `e${i}`,
        iteration_n: i + 1,
        status: 'exited',
      }));
      const expanded = headerWidth + Math.min(max, 50) * chipWidth;
      const nodes: Node<FlowNodeData>[] = [
        { ...loopNode('l', iter, 50), width: expanded, height: 120 },
      ];
      render(<FlowGraph nodes={nodes} edges={[]} autoLayout={true} />);
      const passed = lastReactFlowProps!.nodes as Array<Node<FlowNodeData> & { width?: number }>;
      expect(passed[0].width).toBe(headerWidth + max * chipWidth);
    });
  });

  describe('ELK layout engine wiring', () => {
    // FlowGraph reads the layout engine from window.location.search on
    // each render. The default jsdom URL has no query string -> ELK is
    // the default. Tests that want the dagre branch use
    // history.replaceState to set ?layout=dagre BEFORE rendering and
    // restore on cleanup.

    function setSearch(search: string): void {
      window.history.replaceState(null, '', `${window.location.pathname}${search}`);
    }
    function clearSearch(): void {
      window.history.replaceState(null, '', window.location.pathname);
    }

    test('uses ELK by default (calls applyElkLayout for autoLayout graphs)', async () => {
      clearSearch();
      const elkMod = await import('../../lib/elkLayout');
      const spy = vi.spyOn(elkMod, 'applyElkLayout').mockResolvedValue({
        nodePositions: new Map([
          ['a', { x: 0, y: 0 }],
          ['b', { x: 0, y: 100 }],
        ]),
        edgeRouting: new Map([
          [
            'a-b',
            {
              sections: [
                { id: 's0', startPoint: { x: 50, y: 50 }, endPoint: { x: 50, y: 150 } },
              ],
              labelPos: { x: 60, y: 100 },
            },
          ],
        ]),
      });
      const nodes: Node<FlowNodeData>[] = [
        { id: 'a', position: { x: 0, y: 0 }, data: { kind: 'state', label: 'a' } },
        { id: 'b', position: { x: 0, y: 0 }, data: { kind: 'state', label: 'b' } },
      ];
      const edges: Edge[] = [{ id: 'a-b', source: 'a', target: 'b' }];
      render(<FlowGraph nodes={nodes} edges={edges} autoLayout={true} />);
      // applyElkLayout is invoked asynchronously inside a useEffect;
      // wait one microtask for the promise to resolve.
      await act(async () => {
        await Promise.resolve();
      });
      expect(spy).toHaveBeenCalled();
      spy.mockRestore();
    });

    test('uses dagre when ?layout=dagre is present (does NOT call applyElkLayout)', async () => {
      setSearch('?layout=dagre');
      try {
        const elkMod = await import('../../lib/elkLayout');
        const spy = vi.spyOn(elkMod, 'applyElkLayout').mockResolvedValue({
          nodePositions: new Map(),
          edgeRouting: new Map(),
        });
        const nodes: Node<FlowNodeData>[] = [
          { id: 'a', position: { x: 0, y: 0 }, data: { kind: 'state', label: 'a' } },
          { id: 'b', position: { x: 0, y: 0 }, data: { kind: 'state', label: 'b' } },
        ];
        const edges: Edge[] = [{ id: 'a-b', source: 'a', target: 'b' }];
        render(<FlowGraph nodes={nodes} edges={edges} autoLayout={true} />);
        await act(async () => {
          await Promise.resolve();
        });
        expect(spy).not.toHaveBeenCalled();
        // The wrapper exposes the active engine for e2e tests to query.
        const wrapper = document.querySelector('.flow-graph') as HTMLElement;
        expect(wrapper.getAttribute('data-layout-engine')).toBe('dagre');
        // Dagre output (positions differ across the two nodes) is what
        // was handed to ReactFlow.
        const positioned = lastReactFlowProps!.nodes as Node<FlowNodeData>[];
        const ys = positioned.map((n) => n.position.y);
        expect(new Set(ys).size).toBe(2);
        spy.mockRestore();
      } finally {
        clearSearch();
      }
    });

    test('default wrapper data-layout-engine is "elk"', () => {
      clearSearch();
      const nodes: Node<FlowNodeData>[] = [
        { id: 'a', position: { x: 0, y: 0 }, data: { kind: 'state', label: 'a' } },
      ];
      render(<FlowGraph nodes={nodes} edges={[]} />);
      const wrapper = document.querySelector('.flow-graph') as HTMLElement;
      expect(wrapper.getAttribute('data-layout-engine')).toBe('elk');
    });

    test('shows spinner overlay while ELK promise is pending; removes it when resolved', async () => {
      clearSearch();
      const elkMod = await import('../../lib/elkLayout');
      // Build a promise we resolve manually so we can observe both
      // the in-flight + post-resolve states.
      let resolveLayout: (r: import('../../lib/elkLayout').LayoutResult) => void = () => {};
      const pendingPromise = new Promise<import('../../lib/elkLayout').LayoutResult>(
        (r) => {
          resolveLayout = r;
        },
      );
      const spy = vi.spyOn(elkMod, 'applyElkLayout').mockReturnValue(pendingPromise);
      const nodes: Node<FlowNodeData>[] = [
        { id: 'a', position: { x: 0, y: 0 }, data: { kind: 'state', label: 'a' } },
      ];
      const edges: Edge[] = [];
      const { queryByTestId } = render(
        <FlowGraph nodes={nodes} edges={edges} autoLayout={true} />,
      );
      // Spinner should be present while the promise is pending.
      expect(queryByTestId('fsm-graph-spinner')).not.toBeNull();
      // Resolve the promise and wait for the effect cleanup to run.
      await act(async () => {
        resolveLayout({
          nodePositions: new Map([['a', { x: 0, y: 0 }]]),
          edgeRouting: new Map(),
        });
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(queryByTestId('fsm-graph-spinner')).toBeNull();
      spy.mockRestore();
    });

    test('falls back to dagre when applyElkLayout throws (data-layout-fallback=dagre)', async () => {
      clearSearch();
      const elkMod = await import('../../lib/elkLayout');
      const spy = vi
        .spyOn(elkMod, 'applyElkLayout')
        .mockRejectedValue(new Error('synthetic ELK failure'));
      const consoleWarn = vi.spyOn(console, 'warn').mockImplementation(() => {});
      const nodes: Node<FlowNodeData>[] = [
        { id: 'a', position: { x: 0, y: 0 }, data: { kind: 'state', label: 'a' } },
        { id: 'b', position: { x: 0, y: 0 }, data: { kind: 'state', label: 'b' } },
      ];
      const edges: Edge[] = [{ id: 'a-b', source: 'a', target: 'b' }];
      render(<FlowGraph nodes={nodes} edges={edges} autoLayout={true} />);
      // Drain microtasks so the rejection + setState lands.
      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
      });
      const wrapper = document.querySelector('.flow-graph') as HTMLElement;
      expect(wrapper.getAttribute('data-layout-fallback')).toBe('dagre');
      // The dagre useMemo result is what got forwarded to ReactFlow.
      const positioned = lastReactFlowProps!.nodes as Node<FlowNodeData>[];
      const ys = positioned.map((n) => n.position.y);
      expect(new Set(ys).size).toBe(2);
      spy.mockRestore();
      consoleWarn.mockRestore();
    });
  });

  test('default direction is TB (top-to-bottom) when not specified', () => {
    const nodes: Node<FlowNodeData>[] = [
      { id: 'a', position: { x: 0, y: 0 }, data: { kind: 'state', label: 'a' } },
      { id: 'b', position: { x: 0, y: 0 }, data: { kind: 'state', label: 'b' } },
    ];
    const edges: Edge[] = [{ id: 'a-b', source: 'a', target: 'b' }];
    // No direction prop. With TB, dagre stacks ranks vertically, so the
    // y coordinates differ but the x coordinates align.
    render(<FlowGraph nodes={nodes} edges={edges} autoLayout={true} />);
    const positioned = lastReactFlowProps!.nodes as Node<FlowNodeData>[];
    const ys = positioned.map((n) => n.position.y);
    expect(new Set(ys).size).toBe(2);
  });
});
