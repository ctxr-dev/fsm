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
import { cleanup, render } from '@testing-library/preact';

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
  Position: { Left: 'left', Right: 'right', Top: 'top', Bottom: 'bottom' },
  BackgroundVariant: { Dots: 'dots', Lines: 'lines', Cross: 'cross' },
  MarkerType: { ArrowClosed: 'arrowclosed', Arrow: 'arrow' },
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

  test('autoLayout=false passes nodes through untouched', () => {
    const nodes: Node<FlowNodeData>[] = [
      { id: 'a', position: { x: 100, y: 200 }, data: { kind: 'state', label: 'a' } },
    ];
    render(<FlowGraph nodes={nodes} edges={[]} autoLayout={false} />);
    const passed = lastReactFlowProps!.nodes as Node<FlowNodeData>[];
    expect(passed[0].position).toEqual({ x: 100, y: 200 });
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

  test('decorateEdges truncates labels longer than LABEL_MAX_CHARS', () => {
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
    expect(lbl.length).toBeLessThanOrEqual(28);
    expect(lbl.endsWith('…')).toBe(true);
    expect(longLabel.startsWith(lbl.slice(0, -1))).toBe(true);
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
