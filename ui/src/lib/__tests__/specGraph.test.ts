/**
 * Tests for lib/specGraph.ts.
 */

import { describe, expect, test } from 'vitest';

import { specToGraph } from '../specGraph';

describe('specToGraph', () => {
  test('null / undefined / non-object input → empty graph', () => {
    expect(specToGraph(null)).toEqual({ nodes: [], edges: [] });
    expect(specToGraph(undefined)).toEqual({ nodes: [], edges: [] });
    expect(specToGraph(42)).toEqual({ nodes: [], edges: [] });
  });

  test('empty states array → empty graph', () => {
    expect(specToGraph({ id: 'x', states: [] })).toEqual({ nodes: [], edges: [] });
  });

  test('one state with no transitions → 1 node, 0 edges, kind=terminal', () => {
    const g = specToGraph({ id: 'x', entry: 'a', states: [{ id: 'a' }] });
    expect(g.nodes).toHaveLength(1);
    expect(g.nodes[0].data.kind).toBe('terminal');
    expect(g.edges).toHaveLength(0);
  });

  test('linear chain a → b → c yields 3 nodes + 2 edges', () => {
    const g = specToGraph({
      id: 'x',
      entry: 'a',
      states: [
        { id: 'a', worker: { role: 'r' }, transitions: [{ to: 'b', when: 'always' }] },
        { id: 'b', worker: { role: 'r' }, transitions: [{ to: 'c', when: 'always' }] },
        { id: 'c' },
      ],
    });
    expect(g.nodes.map((n) => n.id)).toEqual(['a', 'b', 'c']);
    expect(g.edges.map((e) => `${e.source}->${e.target}`)).toEqual(['a->b', 'b->c']);
  });

  test('worker state gets kind=worker + role sublabel', () => {
    const g = specToGraph({
      states: [{ id: 's', worker: { role: 'planner' }, transitions: [] }],
    });
    expect(g.nodes[0].data.kind).toBe('worker');
    expect(g.nodes[0].data.sublabel).toContain('planner');
  });

  test('inline state gets kind=inline + handler sublabel', () => {
    const g = specToGraph({
      states: [{ id: 's', inline: { handler_id: 'aggregate' }, transitions: [] }],
    });
    expect(g.nodes[0].data.kind).toBe('inline');
    expect(g.nodes[0].data.sublabel).toContain('aggregate');
  });

  test('explicit state.kind overrides inference', () => {
    const g = specToGraph({
      states: [
        { id: 'x', kind: 'terminal', worker: { role: 'r' }, transitions: [] },
      ],
    });
    expect(g.nodes[0].data.kind).toBe('terminal');
  });

  test('entry state gets "entry" prefix in sublabel', () => {
    const g = specToGraph({
      entry: 'start',
      states: [{ id: 'start', worker: { role: 'r' }, transitions: [] }],
    });
    expect(g.nodes[0].data.sublabel).toContain('entry');
  });

  test('transition with string when becomes edge label', () => {
    const g = specToGraph({
      states: [
        { id: 'a', transitions: [{ to: 'b', when: 'verdict == "GO"' }] },
        { id: 'b' },
      ],
    });
    expect(g.edges[0].label).toBe('verdict == "GO"');
  });

  test('transition with object when extracts predicate or expression', () => {
    const g = specToGraph({
      states: [
        { id: 'a', transitions: [{ to: 'b', when: { predicate: 'x > 1' } }] },
        { id: 'b' },
      ],
    });
    expect(g.edges[0].label).toBe('x > 1');
  });

  test('branching: multiple transitions from one state', () => {
    const g = specToGraph({
      states: [
        {
          id: 'a',
          transitions: [
            { to: 'b', when: 'pass' },
            { to: 'c', when: 'fail' },
          ],
        },
        { id: 'b' },
        { id: 'c' },
      ],
    });
    expect(g.edges).toHaveLength(2);
  });

  test('cycle: a → b → a renders without crashing', () => {
    const g = specToGraph({
      states: [
        { id: 'a', transitions: [{ to: 'b', when: 'always' }] },
        { id: 'b', transitions: [{ to: 'a', when: 'always' }] },
      ],
    });
    expect(g.nodes).toHaveLength(2);
    expect(g.edges).toHaveLength(2);
  });

  test('idempotent: same input produces same output', () => {
    const input = {
      entry: 'a',
      states: [{ id: 'a', worker: { role: 'r' }, transitions: [{ to: 'b', when: 'always' }] }, { id: 'b' }],
    };
    const a = specToGraph(input);
    const b = specToGraph(input);
    expect(a.nodes).toEqual(b.nodes);
    expect(a.edges.map(({ id: _id, ...rest }) => rest)).toEqual(
      b.edges.map(({ id: _id, ...rest }) => rest),
    );
  });
});
