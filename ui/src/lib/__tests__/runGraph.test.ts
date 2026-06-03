/**
 * Tests for lib/runGraph.ts — buildRunOverlay + overlayRunOnSpecGraph
 * + overlayProgress.
 *
 * Covers (per Copilot review on PR #62):
 *   - branching trees (one parent → two children = two taken edges,
 *     not a sibling-to-sibling transition)
 *   - loops (revisiting the same state via the engine's parent->child
 *     relation)
 *   - fault precedence (faulted beats exited beats entered beats
 *     not_visited in strongerStatus)
 *   - taken-transition derivation from parent->child links
 *   - status derivation from exited_at (the timestamp is the engine's
 *     source of truth; unknown status strings fall through to the
 *     timestamp-based branch instead of being silently classified as
 *     "entered")
 *   - overlayRunOnSpecGraph plumbs data.runStatus + data.isCurrent
 *     onto every node (FsmNode reads them) and DOES NOT set
 *     node.style (which the FsmNode card ignored, producing the
 *     invisible-overlay bug)
 */

import { describe, expect, test } from 'vitest';

import type { Event as FsmEvent, RunManifest, StateNode } from '../api';
import type { FlowNodeData } from '../../components/FlowGraph';
import type { Edge, Node } from '@xyflow/react';
import {
  buildRunOverlay,
  overlayProgress,
  overlayRunOnSpecGraph,
} from '../runGraph';

/** Minimal StateNode factory — fills the required fields with sane
 *  defaults so individual tests only have to specify the bits that
 *  matter. */
function node(
  state_id: string,
  opts: Partial<StateNode> = {},
): StateNode {
  return {
    entry_id: opts.entry_id ?? `entry-${state_id}-${opts.entry_seq ?? 0}`,
    state_id,
    entry_seq: opts.entry_seq ?? 0,
    entered_at: opts.entered_at ?? '2026-01-01T00:00:00Z',
    exited_at: opts.exited_at ?? null,
    status: opts.status ?? 'entered',
    inputs: opts.inputs ?? {},
    outputs: opts.outputs ?? {},
    iteration_n: opts.iteration_n ?? null,
    children: opts.children ?? [],
  };
}

/** Minimal RunManifest stub — only ``current_state`` is read by
 *  buildRunOverlay; the rest is required by the type. */
function manifest(currentState: string | null): RunManifest {
  return {
    id: 'run-1',
    project_id: 'proj-1',
    fsm_spec_id: 'spec-1',
    fsm_spec_hash: 'h',
    status: 'running',
    current_state: currentState,
    next_state: null,
    verdict: null,
    started_at: '2026-01-01T00:00:00Z',
    ended_at: null,
    last_update_at: '2026-01-01T00:00:00Z',
    paused_at: null,
    pause_reason: null,
    parent_run_id: null,
    resume_history: [],
    args: {},
    metadata: {},
    transitions_count: 0,
  };
}

const noEvents: FsmEvent[] = [];

describe('buildRunOverlay', () => {
  test('null state tree → empty overlay (no faults, no transitions)', () => {
    const o = buildRunOverlay(manifest(null), null, noEvents);
    expect(o.statusByStateId.size).toBe(0);
    expect(o.takenTransitions.size).toBe(0);
    expect(o.faultedStateId).toBeNull();
    expect(o.currentStateId).toBeNull();
  });

  test('currentStateId mirrors manifest.current_state', () => {
    const o = buildRunOverlay(manifest('b'), null, noEvents);
    expect(o.currentStateId).toBe('b');
  });

  test('linear chain a → b → c marks all three statuses correctly', () => {
    const tree = node('a', {
      entry_seq: 0,
      exited_at: '2026-01-01T00:00:01Z',
      status: 'exited',
      children: [
        node('b', {
          entry_seq: 1,
          exited_at: '2026-01-01T00:00:02Z',
          status: 'exited',
          children: [
            node('c', { entry_seq: 2, exited_at: null, status: 'entered' }),
          ],
        }),
      ],
    });
    const o = buildRunOverlay(manifest('c'), tree, noEvents);
    expect(o.statusByStateId.get('a')).toBe('exited');
    expect(o.statusByStateId.get('b')).toBe('exited');
    expect(o.statusByStateId.get('c')).toBe('entered');
    expect(Array.from(o.takenTransitions).sort()).toEqual(['a::b', 'b::c']);
  });

  test('branching: parent with two children emits both taken edges and NO sibling-to-sibling edge', () => {
    const tree = node('root', {
      entry_seq: 0,
      exited_at: '2026-01-01T00:00:01Z',
      status: 'exited',
      children: [
        node('left', { entry_seq: 1, exited_at: '2026-01-01T00:00:02Z', status: 'exited' }),
        node('right', { entry_seq: 2, exited_at: null, status: 'entered' }),
      ],
    });
    const o = buildRunOverlay(manifest('right'), tree, noEvents);
    expect(o.takenTransitions.has('root::left')).toBe(true);
    expect(o.takenTransitions.has('root::right')).toBe(true);
    // Critical: left and right are siblings, NOT a transition between
    // them. The pre-fix adjacency-on-flattened-list approach would
    // have added 'left::right' which never happened in the run.
    expect(o.takenTransitions.has('left::right')).toBe(false);
    expect(o.takenTransitions.has('right::left')).toBe(false);
  });

  test('loop: revisiting same state via a child edge is recorded once', () => {
    // a -> b -> a (loop iteration). Tree mirrors the engine's
    // parent/child relation: outer a has child b; b has child a (the
    // loop hop).
    const tree = node('a', {
      entry_seq: 0,
      exited_at: '2026-01-01T00:00:01Z',
      status: 'exited',
      children: [
        node('b', {
          entry_seq: 1,
          exited_at: '2026-01-01T00:00:02Z',
          status: 'exited',
          children: [
            node('a', { entry_seq: 2, exited_at: null, status: 'entered' }),
          ],
        }),
      ],
    });
    const o = buildRunOverlay(manifest('a'), tree, noEvents);
    expect(o.takenTransitions.has('a::b')).toBe(true);
    expect(o.takenTransitions.has('b::a')).toBe(true);
    // The loop revisit means ``a`` was both exited (first iteration)
    // and entered (second). strongerStatus promotes entered over
    // exited, so the merged status is "entered".
    expect(o.statusByStateId.get('a')).toBe('entered');
  });

  test('fault precedence: a faulted iteration wins over an earlier exited iteration', () => {
    // a was entered, exited cleanly, then re-entered and faulted.
    const tree = node('a', {
      entry_seq: 0,
      exited_at: '2026-01-01T00:00:01Z',
      status: 'exited',
      children: [
        node('a', {
          entry_seq: 1,
          exited_at: '2026-01-01T00:00:02Z',
          status: 'faulted',
        }),
      ],
    });
    const o = buildRunOverlay(manifest('a'), tree, noEvents);
    expect(o.statusByStateId.get('a')).toBe('faulted');
    expect(o.faultedStateId).toBe('a');
  });

  test('faultedStateId picks the FIRST faulted entry encountered', () => {
    const tree = node('root', {
      entry_seq: 0,
      exited_at: '2026-01-01T00:00:01Z',
      status: 'exited',
      children: [
        node('first', {
          entry_seq: 1,
          exited_at: '2026-01-01T00:00:02Z',
          status: 'faulted',
        }),
        node('second', {
          entry_seq: 2,
          exited_at: '2026-01-01T00:00:03Z',
          status: 'faulted',
        }),
      ],
    });
    const o = buildRunOverlay(manifest(null), tree, noEvents);
    expect(o.faultedStateId).toBe('first');
  });

  test('status derivation: exited_at=null marks "entered" regardless of status string', () => {
    // The engine occasionally emits forward-compatible status strings
    // (e.g. "suspended") that should NOT be misclassified as
    // "entered" purely because they're unknown — but if exited_at is
    // null the state IS still active, so "entered" is correct.
    const tree = node('s', { entry_seq: 0, exited_at: null, status: 'something_new' });
    const o = buildRunOverlay(manifest(null), tree, noEvents);
    expect(o.statusByStateId.get('s')).toBe('entered');
  });

  test('status derivation: exited_at set + non-faulted status → "exited"', () => {
    // Unknown / future status string on a closed entry must NOT be
    // misclassified as still-active. The pre-fix shortcut fell back
    // to "entered" for anything that wasn't literally "exited" /
    // "faulted"; the timestamp-based derivation keeps it correct.
    const tree = node('s', {
      entry_seq: 0,
      exited_at: '2026-01-01T00:00:01Z',
      status: 'completed',
    });
    const o = buildRunOverlay(manifest(null), tree, noEvents);
    expect(o.statusByStateId.get('s')).toBe('exited');
  });

  test('status derivation: "faulted" string wins even when exited_at is set', () => {
    const tree = node('s', {
      entry_seq: 0,
      exited_at: '2026-01-01T00:00:01Z',
      status: 'faulted',
    });
    const o = buildRunOverlay(manifest(null), tree, noEvents);
    expect(o.statusByStateId.get('s')).toBe('faulted');
  });

  test('entry_seq drives chronological order even when children are listed out of order', () => {
    const tree = node('a', {
      entry_seq: 0,
      exited_at: '2026-01-01T00:00:01Z',
      status: 'exited',
      children: [
        // Children listed in non-chronological order; entry_seq=2
        // first, entry_seq=1 second. flattenEntries sorts by
        // entry_seq so the older one is processed first.
        node('later', { entry_seq: 2, exited_at: null, status: 'entered' }),
        node('earlier', { entry_seq: 1, exited_at: '2026-01-01T00:00:02Z', status: 'exited' }),
      ],
    });
    const o = buildRunOverlay(manifest('later'), tree, noEvents);
    expect(o.statusByStateId.get('earlier')).toBe('exited');
    expect(o.statusByStateId.get('later')).toBe('entered');
  });
});

describe('overlayRunOnSpecGraph', () => {
  function specNode(id: string, label = id): Node<FlowNodeData> {
    return {
      id,
      position: { x: 0, y: 0 },
      data: { kind: 'state', label } as FlowNodeData,
    };
  }
  function specEdge(source: string, target: string): Edge {
    return { id: `${source}-${target}`, source, target };
  }

  test('overlay plumbs data.runStatus + data.isCurrent onto every node', () => {
    const base = {
      nodes: [specNode('a'), specNode('b'), specNode('c')],
      edges: [specEdge('a', 'b'), specEdge('b', 'c')],
    };
    const tree = node('a', {
      entry_seq: 0,
      exited_at: '2026-01-01T00:00:01Z',
      status: 'exited',
      children: [
        node('b', { entry_seq: 1, exited_at: null, status: 'entered' }),
      ],
    });
    const overlay = buildRunOverlay(manifest('b'), tree, noEvents);
    const out = overlayRunOnSpecGraph(base, overlay);
    const dataById = new Map(out.nodes.map((n) => [n.id, n.data as FlowNodeData & { runStatus?: string; isCurrent?: boolean }]));
    expect(dataById.get('a')?.runStatus).toBe('exited');
    expect(dataById.get('a')?.isCurrent).toBe(false);
    expect(dataById.get('b')?.runStatus).toBe('entered');
    expect(dataById.get('b')?.isCurrent).toBe(true);
    expect(dataById.get('c')?.runStatus).toBe('not_visited');
    expect(dataById.get('c')?.isCurrent).toBe(false);
  });

  test('overlay does NOT set node.style — colour-coding flows through data.runStatus only', () => {
    // Regression for the Copilot finding: setting border/background
    // on node.style produced no visible overlay because FsmNode
    // ignores the React Flow wrapper's inline styles. The overlay
    // now relies entirely on FsmNode reading data.runStatus.
    const base = {
      nodes: [specNode('a')],
      edges: [] as Edge[],
    };
    const tree = node('a', { entry_seq: 0, exited_at: null, status: 'entered' });
    const overlay = buildRunOverlay(manifest('a'), tree, noEvents);
    const out = overlayRunOnSpecGraph(base, overlay);
    // node.style is either undefined or empty — but most importantly
    // the overlay must not be setting border/background/boxShadow.
    const style = (out.nodes[0].style ?? {}) as Record<string, unknown>;
    expect(style.border).toBeUndefined();
    expect(style.background).toBeUndefined();
    expect(style.boxShadow).toBeUndefined();
  });

  test('current state with no faulted status is rendered as entered (visual override)', () => {
    // Even if the recorded status is "exited" because of a previous
    // iteration, currentStateId === stateId forces the visual back to
    // "entered" so the operator's eye lands on the active state.
    const base = { nodes: [specNode('a')], edges: [] as Edge[] };
    const tree = node('a', {
      entry_seq: 0,
      exited_at: '2026-01-01T00:00:01Z',
      status: 'exited',
    });
    const overlay = buildRunOverlay(manifest('a'), tree, noEvents);
    const out = overlayRunOnSpecGraph(base, overlay);
    const data = out.nodes[0].data as FlowNodeData & { runStatus?: string; isCurrent?: boolean };
    expect(data.runStatus).toBe('entered');
    expect(data.isCurrent).toBe(true);
  });

  test('faulted status survives the current-state override', () => {
    // If the current state also faulted, the fault visual wins so
    // the operator sees the failure, not just "active".
    const base = { nodes: [specNode('a')], edges: [] as Edge[] };
    const tree = node('a', {
      entry_seq: 0,
      exited_at: '2026-01-01T00:00:01Z',
      status: 'faulted',
    });
    const overlay = buildRunOverlay(manifest('a'), tree, noEvents);
    const out = overlayRunOnSpecGraph(base, overlay);
    const data = out.nodes[0].data as FlowNodeData & { runStatus?: string; isCurrent?: boolean };
    expect(data.runStatus).toBe('faulted');
    expect(data.isCurrent).toBe(true);
  });

  test('taken edges get green stroke + bold label; untaken stay muted', () => {
    const base = {
      nodes: [specNode('a'), specNode('b'), specNode('c')],
      edges: [specEdge('a', 'b'), specEdge('a', 'c')],
    };
    const tree = node('a', {
      entry_seq: 0,
      exited_at: '2026-01-01T00:00:01Z',
      status: 'exited',
      children: [node('b', { entry_seq: 1, exited_at: null, status: 'entered' })],
    });
    const overlay = buildRunOverlay(manifest('b'), tree, noEvents);
    const out = overlayRunOnSpecGraph(base, overlay);
    const ab = out.edges.find((e) => e.source === 'a' && e.target === 'b');
    const ac = out.edges.find((e) => e.source === 'a' && e.target === 'c');
    expect((ab?.style as Record<string, unknown>)?.stroke).toBe('#10b981');
    expect((ac?.style as Record<string, unknown>)?.stroke).toBe('#cbd5e1');
    expect((ab?.labelStyle as Record<string, unknown>)?.fontWeight).toBe(600);
    expect((ac?.labelStyle as Record<string, unknown>)?.fontWeight).toBe(400);
  });

  test('badge prefix is composed into data.label and is idempotent', () => {
    const base = { nodes: [specNode('a', 'alpha')], edges: [] as Edge[] };
    const tree = node('a', { entry_seq: 0, exited_at: null, status: 'entered' });
    const overlay = buildRunOverlay(manifest('a'), tree, noEvents);
    const first = overlayRunOnSpecGraph(base, overlay);
    expect(first.nodes[0].data.label).toBe('▸ alpha');
    // Re-applying overlay onto the SAME base must not stack prefixes.
    const second = overlayRunOnSpecGraph(base, overlay);
    expect(second.nodes[0].data.label).toBe('▸ alpha');
  });
});

describe('PR 5: loop iteration entries', () => {
  test('buildRunOverlay collects iteration entries per state_id in chronological order', () => {
    // 3 iterations of state "tick" — entry_seq drives the chip order.
    const tree = node('loop_root', {
      entry_seq: 0,
      exited_at: '2026-01-01T00:00:01Z',
      status: 'exited',
      children: [
        node('tick', {
          entry_id: 'tick-1',
          entry_seq: 1,
          iteration_n: 1,
          exited_at: '2026-01-01T00:00:02Z',
          status: 'exited',
          children: [
            node('tick', {
              entry_id: 'tick-2',
              entry_seq: 2,
              iteration_n: 2,
              exited_at: '2026-01-01T00:00:03Z',
              status: 'exited',
              children: [
                node('tick', {
                  entry_id: 'tick-3',
                  entry_seq: 3,
                  iteration_n: 3,
                  exited_at: null,
                  status: 'entered',
                }),
              ],
            }),
          ],
        }),
      ],
    });
    const o = buildRunOverlay(manifest('tick'), tree, noEvents);
    const bucket = o.iterationEntriesByStateId.get('tick');
    expect(bucket).toBeDefined();
    expect(bucket!.map((e) => e.entry_id)).toEqual(['tick-1', 'tick-2', 'tick-3']);
    expect(bucket!.map((e) => e.iteration_n)).toEqual([1, 2, 3]);
    expect(bucket!.map((e) => e.status)).toEqual(['exited', 'exited', 'entered']);
  });

  test('buildRunOverlay returns empty bucket for an unvisited state', () => {
    const o = buildRunOverlay(manifest(null), null, noEvents);
    expect(o.iterationEntriesByStateId.get('never_entered')).toBeUndefined();
  });

  test('overlayRunOnSpecGraph plumbs iterationCount + iterationEntries onto a loop node', () => {
    const specNode = (id: string, opts: Partial<FlowNodeData> = {}): Node<FlowNodeData> => ({
      id,
      position: { x: 0, y: 0 },
      data: { kind: 'loop', label: id, isLoop: true, loopMaxIterations: 10, ...opts } as FlowNodeData,
    });
    const base = {
      nodes: [specNode('tick')],
      edges: [] as Edge[],
    };
    const tree = node('tick', {
      entry_id: 'tick-1',
      entry_seq: 1,
      iteration_n: 1,
      exited_at: '2026-01-01T00:00:01Z',
      status: 'exited',
      children: [
        node('tick', {
          entry_id: 'tick-2',
          entry_seq: 2,
          iteration_n: 2,
          exited_at: null,
          status: 'entered',
        }),
      ],
    });
    const overlay = buildRunOverlay(manifest('tick'), tree, noEvents);
    const out = overlayRunOnSpecGraph(base, overlay);
    const data = out.nodes[0].data as FlowNodeData & {
      iterationCount?: number;
      iterationEntries?: Array<{ entry_id: string; iteration_n: number | null; status: string }>;
    };
    expect(data.iterationCount).toBe(2);
    expect(data.iterationEntries?.map((e) => e.entry_id)).toEqual(['tick-1', 'tick-2']);
    expect(data.iterationEntries?.map((e) => e.iteration_n)).toEqual([1, 2]);
  });

  test('overlayRunOnSpecGraph re-stamps loop node width when iterations exceed spec ceiling', () => {
    const specNode = (id: string): Node<FlowNodeData> => ({
      id,
      position: { x: 0, y: 0 },
      width: 270 + 2 * 40, // spec said max 2
      data: { kind: 'loop', label: id, isLoop: true, loopMaxIterations: 2 } as FlowNodeData,
    });
    const base = { nodes: [specNode('tick')], edges: [] as Edge[] };
    // The run actually ran 5 iterations (engine bumped past declared max).
    let chain: StateNode | null = null;
    for (let i = 5; i >= 1; i -= 1) {
      const opts: Partial<StateNode> = {
        entry_id: `tick-${i}`,
        entry_seq: i,
        iteration_n: i,
        exited_at: i === 5 ? null : '2026-01-01T00:00:01Z',
        status: i === 5 ? 'entered' : 'exited',
        children: chain ? [chain] : [],
      };
      chain = node('tick', opts);
    }
    const overlay = buildRunOverlay(manifest('tick'), chain, noEvents);
    const out = overlayRunOnSpecGraph(base, overlay);
    // Width should expand to fit the OBSERVED 5 iterations: 270 + 5*40.
    expect(out.nodes[0].width).toBe(270 + 5 * 40);
  });
});

describe('overlayProgress', () => {
  test('counts visited and faulted distinctly from total', () => {
    const tree = node('a', {
      entry_seq: 0,
      exited_at: '2026-01-01T00:00:01Z',
      status: 'exited',
      children: [
        node('b', { entry_seq: 1, exited_at: '2026-01-01T00:00:02Z', status: 'faulted' }),
      ],
    });
    const overlay = buildRunOverlay(manifest(null), tree, noEvents);
    const p = overlayProgress(overlay, 5);
    expect(p.visited).toBe(2);
    expect(p.faulted).toBe(1);
    expect(p.total).toBe(5);
  });

  test('not-visited states are not counted as visited', () => {
    const tree = node('a', { entry_seq: 0, exited_at: null, status: 'entered' });
    const overlay = buildRunOverlay(manifest('a'), tree, noEvents);
    const p = overlayProgress(overlay, 3);
    expect(p.visited).toBe(1);
    expect(p.faulted).toBe(0);
    expect(p.total).toBe(3);
  });
});
