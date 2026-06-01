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

  // -------------------------------------------------------------------------
  // W21: data carriers for the click-Sheet inspectors.
  // -------------------------------------------------------------------------

  test('W21: node.data.state carries the original spec state object', () => {
    const stateA = {
      id: 'a',
      worker: { role: 'planner', prompt_template: 'plan this', allowed_tools: ['x'] },
      transitions: [{ to: 'b', when: 'always' }],
    };
    const g = specToGraph({ entry: 'a', states: [stateA, { id: 'b' }] });
    expect(g.nodes[0].data.state).toBeDefined();
    const carried = g.nodes[0].data.state as Record<string, unknown>;
    expect(carried.id).toBe('a');
    expect(carried.worker).toEqual(stateA.worker);
    expect(carried.transitions).toEqual(stateA.transitions);
  });

  test('W21: node.data.fullLabel mirrors the state id', () => {
    const g = specToGraph({
      states: [{ id: 'risk_tier_triage_super_long_name', inline: { handler_id: 'h' } }],
    });
    expect(g.nodes[0].data.fullLabel).toBe('risk_tier_triage_super_long_name');
  });

  test('W21: node.data.fullSublabel carries the un-projected sublabel', () => {
    const g = specToGraph({
      states: [{ id: 's', worker: { role: 'specialist' }, transitions: [] }],
    });
    expect(g.nodes[0].data.fullSublabel).toContain('specialist');
  });

  test('W21: entry-state fullSublabel mirrors the visible "entry · ..." string', () => {
    const g = specToGraph({
      entry: 's',
      states: [{ id: 's', worker: { role: 'planner' } }],
    });
    expect(g.nodes[0].data.fullSublabel).toBe(g.nodes[0].data.sublabel);
    expect(g.nodes[0].data.fullSublabel).toContain('entry');
  });

  test('W21: edge.data carries kind, transition, source, target, fullLabel', () => {
    const longPredicate = "tier == 'trivial' AND len(risk_signals) == 0 AND NOT scope_overrides_present";
    const g = specToGraph({
      states: [
        { id: 'a', transitions: [{ to: 'b', kind: 'deterministic', when: longPredicate }] },
        { id: 'b' },
      ],
    });
    const data = g.edges[0].data as Record<string, unknown>;
    expect(data).toBeDefined();
    expect(data.fullLabel).toBe(longPredicate);
    expect(data.kind).toBe('deterministic');
    expect(data.sourceId).toBe('a');
    expect(data.targetId).toBe('b');
    expect((data.transition as Record<string, unknown>).to).toBe('b');
  });

  test('W21: edge.data.fullLabel undefined when transition has no predicate', () => {
    const g = specToGraph({
      states: [
        { id: 'a', transitions: [{ to: 'b' }] },
        { id: 'b' },
      ],
    });
    const data = g.edges[0].data as Record<string, unknown>;
    expect(data.fullLabel).toBeUndefined();
  });

  // -------------------------------------------------------------------------
  // W21 follow-up: kind derived from `when` (not top-level t.kind);
  // criteria fallback for judgement transitions.
  // -------------------------------------------------------------------------

  test('W21: edge.data.kind for bare "always" string transition', () => {
    const g = specToGraph({
      states: [{ id: 'a', transitions: [{ to: 'b', when: 'always' }] }, { id: 'b' }],
    });
    expect((g.edges[0].data as Record<string, unknown>).kind).toBe('always');
  });

  test('W21: edge.data.kind for bare "otherwise" string transition', () => {
    const g = specToGraph({
      states: [{ id: 'a', transitions: [{ to: 'b', when: 'otherwise' }] }, { id: 'b' }],
    });
    expect((g.edges[0].data as Record<string, unknown>).kind).toBe('otherwise');
  });

  test('W21: edge.data.kind for {kind:"judgement", criteria:"..."} dict-form when', () => {
    const g = specToGraph({
      states: [
        {
          id: 'a',
          transitions: [{ to: 'b', when: { kind: 'judgement', criteria: 'verdict is GO' } }],
        },
        { id: 'b' },
      ],
    });
    const data = g.edges[0].data as Record<string, unknown>;
    expect(data.kind).toBe('judgement');
    // And the fullLabel should fall through to criteria text.
    expect(data.fullLabel).toBe('verdict is GO');
  });

  test('W21: edge.data.kind for {kind:"deterministic", expression:"..."} dict', () => {
    const g = specToGraph({
      states: [
        {
          id: 'a',
          transitions: [{ to: 'b', when: { kind: 'deterministic', expression: 'x > 0' } }],
        },
        { id: 'b' },
      ],
    });
    const data = g.edges[0].data as Record<string, unknown>;
    expect(data.kind).toBe('deterministic');
    expect(data.fullLabel).toBe('x > 0');
  });

  test('W21: bare predicate-string when is treated as deterministic', () => {
    const g = specToGraph({
      states: [
        { id: 'a', transitions: [{ to: 'b', when: 'verdict == "GO"' }] },
        { id: 'b' },
      ],
    });
    const data = g.edges[0].data as Record<string, unknown>;
    expect(data.kind).toBe('deterministic');
  });
});
