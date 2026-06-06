/**
 * Tests for ui/src/lib/elkLayout.ts.
 *
 * ELK is a real layout engine that runs synchronously in the test
 * environment when invoked via elkjs's bundled main-thread fallback
 * (jsdom has no Web Worker support, so elkjs falls back to the
 * inline implementation). The assertions here check STRUCTURAL
 * properties of the result — orthogonal routing, distinct positions
 * for parallel edges, label dimensions round-tripping — rather than
 * pixel positions (which are not byte-deterministic across runs).
 */

import { describe, expect, test } from 'vitest';
import type { Edge, Node } from '@xyflow/react';

import { applyElkLayout } from '../elkLayout';

function makeNode(id: string, width = 270, height = 84): Node {
  return {
    id,
    position: { x: 0, y: 0 },
    data: {},
    width,
    height,
  } as unknown as Node;
}

describe('applyElkLayout', () => {
  test('tiny graph (2 nodes, 1 edge) lays out with populated sections', async () => {
    const nodes: Node[] = [makeNode('a'), makeNode('b')];
    const edges: Edge[] = [{ id: 'a-b', source: 'a', target: 'b' }];

    const result = await applyElkLayout(nodes, edges);

    expect(result.nodePositions.size).toBe(2);
    expect(result.nodePositions.has('a')).toBe(true);
    expect(result.nodePositions.has('b')).toBe(true);

    expect(result.edgeRouting.size).toBe(1);
    const routing = result.edgeRouting.get('a-b');
    expect(routing).toBeDefined();
    expect(routing!.sections.length).toBeGreaterThanOrEqual(1);
    // Every section carries at least a startPoint + endPoint.
    const section = routing!.sections[0];
    expect(typeof section.startPoint.x).toBe('number');
    expect(typeof section.startPoint.y).toBe('number');
    expect(typeof section.endPoint.x).toBe('number');
    expect(typeof section.endPoint.y).toBe('number');
  });

  test('medium graph (15 nodes, 25 edges with labels) lays out; every edge has section + label pos', async () => {
    // Build a layered graph: 15 nodes wired into a depth-3 DAG with
    // 25 edges total. Each edge carries a label dimension so ELK has
    // to reserve label slack.
    const nodes: Node[] = Array.from({ length: 15 }, (_, i) =>
      makeNode(`n${i}`, 200, 80),
    );
    const edges: Edge[] = [];
    // Layer wiring: 5 nodes per layer, fully-connected layer-to-layer
    // up to 25 edges total.
    let edgeIdx = 0;
    for (let layer = 0; layer < 2 && edgeIdx < 25; layer++) {
      for (let src = 0; src < 5 && edgeIdx < 25; src++) {
        for (let tgt = 0; tgt < 5 && edgeIdx < 25; tgt++) {
          const srcId = `n${layer * 5 + src}`;
          const tgtId = `n${(layer + 1) * 5 + tgt}`;
          edges.push({ id: `e${edgeIdx}`, source: srcId, target: tgtId });
          edgeIdx++;
        }
      }
    }

    const labelDimensions = new Map(
      edges.map((e) => [e.id, { width: 120, height: 28 }]),
    );

    const result = await applyElkLayout(nodes, edges, { labelDimensions });

    expect(result.nodePositions.size).toBe(15);
    expect(result.edgeRouting.size).toBe(edges.length);
    for (const e of edges) {
      const routing = result.edgeRouting.get(e.id);
      expect(routing, `routing for ${e.id}`).toBeDefined();
      expect(routing!.sections.length).toBeGreaterThanOrEqual(1);
      // Label position should have been assigned because we passed
      // a non-empty label dimension.
      expect(routing!.labelPos).not.toBeNull();
      expect(typeof routing!.labelPos!.x).toBe('number');
      expect(typeof routing!.labelPos!.y).toBe('number');
    }
  });

  test('orthogonal routing: every bend transition is axis-aligned (no diagonals)', async () => {
    // 4 nodes wired so ELK must produce at least one bend.
    const nodes: Node[] = [
      makeNode('a'),
      makeNode('b'),
      makeNode('c'),
      makeNode('d'),
    ];
    const edges: Edge[] = [
      { id: 'a-b', source: 'a', target: 'b' },
      { id: 'a-c', source: 'a', target: 'c' },
      { id: 'b-d', source: 'b', target: 'd' },
      { id: 'c-d', source: 'c', target: 'd' },
    ];

    const result = await applyElkLayout(nodes, edges);

    // Walk every section. For each section, the polyline is
    // [startPoint, ...bendPoints, endPoint]. ORTHOGONAL routing means
    // every consecutive pair shares either an x OR a y coordinate
    // (i.e. the segment is horizontal-only or vertical-only — never
    // diagonal). Allow small floating-point slack.
    const EPSILON = 0.5;
    for (const [edgeId, routing] of result.edgeRouting) {
      for (const section of routing.sections) {
        const points = [
          section.startPoint,
          ...(section.bendPoints ?? []),
          section.endPoint,
        ];
        for (let i = 0; i < points.length - 1; i++) {
          const p = points[i];
          const q = points[i + 1];
          const dx = Math.abs(q.x - p.x);
          const dy = Math.abs(q.y - p.y);
          const isAxisAligned = dx < EPSILON || dy < EPSILON;
          expect(
            isAxisAligned,
            `edge ${edgeId} section segment ${i} should be axis-aligned: (${p.x},${p.y}) -> (${q.x},${q.y})`,
          ).toBe(true);
        }
      }
    }
  });

  test('multi-edge: two distinct edges A->B get distinct routing entries', async () => {
    const nodes: Node[] = [makeNode('a'), makeNode('b')];
    const edges: Edge[] = [
      { id: 'a-b-0', source: 'a', target: 'b' },
      { id: 'a-b-1', source: 'a', target: 'b' },
    ];

    const result = await applyElkLayout(nodes, edges, {
      labelDimensions: new Map([
        ['a-b-0', { width: 80, height: 20 }],
        ['a-b-1', { width: 80, height: 20 }],
      ]),
    });

    expect(result.edgeRouting.size).toBe(2);
    const r0 = result.edgeRouting.get('a-b-0');
    const r1 = result.edgeRouting.get('a-b-1');
    expect(r0).toBeDefined();
    expect(r1).toBeDefined();
    // Distinctness predicate: either the sections differ (different
    // polyline) OR the label positions differ. ELK is free to route
    // them through the same channel but the label rect reservation
    // makes their label positions distinct in practice.
    const sameSection =
      JSON.stringify(r0!.sections) === JSON.stringify(r1!.sections);
    const sameLabel =
      r0!.labelPos !== null &&
      r1!.labelPos !== null &&
      r0!.labelPos.x === r1!.labelPos.x &&
      r0!.labelPos.y === r1!.labelPos.y;
    expect(
      sameSection && sameLabel,
      'parallel edges must produce distinct routing or distinct label positions',
    ).toBe(false);
  });

  test('direction is pinned to DOWN — caller cannot override to RIGHT', async () => {
    // Vertical orientation is a product-level invariant for both the
    // spec graph and the run graph. Even if a caller passes
    // 'elk.direction': 'RIGHT' in layoutOptions (back-compat surface
    // pre-fix), the wrapper forces DOWN so the layout always renders
    // top-to-bottom. Assertion strategy: lay out a tiny chain with a
    // RIGHT override; the source must be ABOVE the target (positive
    // y delta), not LEFT of it (positive x delta).
    const nodes: Node[] = [makeNode('a', 200, 80), makeNode('b', 200, 80)];
    const edges: Edge[] = [{ id: 'a-b', source: 'a', target: 'b' }];
    const result = await applyElkLayout(nodes, edges, {
      layoutOptions: { 'elk.direction': 'RIGHT' },
    });
    const aPos = result.nodePositions.get('a')!;
    const bPos = result.nodePositions.get('b')!;
    // DOWN: b is below a (yB > yA), x positions align.
    expect(bPos.y).toBeGreaterThan(aPos.y);
    // x alignment: the two cards stack vertically, so |xB - xA| is
    // small (well under one card width) — much smaller than the
    // y-delta which carries a full card + ranksep.
    expect(Math.abs(bPos.x - aPos.x)).toBeLessThan(bPos.y - aPos.y);
  });

  test('label dimensions round-trip: reserved space matches the request', async () => {
    // Single edge with a known label dimension. After layout, the
    // returned label centre should land inside the graph bounding
    // box (i.e. ELK accepted the dimension and gave it a position).
    const nodes: Node[] = [makeNode('a'), makeNode('b')];
    const edges: Edge[] = [{ id: 'a-b', source: 'a', target: 'b' }];

    const dim = { width: 120, height: 28 };
    const result = await applyElkLayout(nodes, edges, {
      labelDimensions: new Map([['a-b', dim]]),
    });

    const routing = result.edgeRouting.get('a-b');
    expect(routing!.labelPos).not.toBeNull();
    const { x: lx, y: ly } = routing!.labelPos!;
    // The label centre should land somewhere between the two nodes
    // (vertically), not on top of a node. With DOWN direction the
    // source is at the top and target is below it.
    const aPos = result.nodePositions.get('a')!;
    const bPos = result.nodePositions.get('b')!;
    const aBottom = aPos.y + 84;
    const bTop = bPos.y;
    // Label centre should sit somewhere between the bottom of source
    // and top of target (with some slack since ELK adds padding).
    // We only assert that the label is NOT inside either node's bbox.
    const labelLeft = lx - dim.width / 2;
    const labelRight = lx + dim.width / 2;
    const labelTop = ly - dim.height / 2;
    const labelBottom = ly + dim.height / 2;
    const insideA =
      labelLeft < aPos.x + 270 &&
      labelRight > aPos.x &&
      labelTop < aPos.y + 84 &&
      labelBottom > aPos.y;
    const insideB =
      labelLeft < bPos.x + 270 &&
      labelRight > bPos.x &&
      labelTop < bPos.y + 84 &&
      labelBottom > bPos.y;
    expect(insideA, 'label must not overlap source node bbox').toBe(false);
    expect(insideB, 'label must not overlap target node bbox').toBe(false);
    // And the label should sit between source and target along the
    // primary direction (DOWN), within the gap.
    expect(ly).toBeGreaterThan(aBottom - 1);
    expect(ly).toBeLessThan(bTop + 1);
  });
});
