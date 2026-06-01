/**
 * Convert an FsmSpec definition (JSON shape) into FlowGraph nodes +
 * edges for FSM visualisation.
 *
 * Input shape (matches ctxr.fsm.core.models.FsmSpec.model_dump):
 *
 *   {
 *     id: "...", version: 1, entry: "state_a",
 *     states: [
 *       { id: "state_a", kind?: "worker"|"loop"|"inline"|"terminal",
 *         worker?: {...}, loop?: {...}, inline?: {...},
 *         transitions: [
 *           { to: "state_b", when: "always"|"otherwise"|{predicate}, kind?: "..." }
 *         ]
 *       }, ...
 *     ]
 *   }
 *
 * Output: nodes typed for `<FlowGraph>` with `data.kind` set to one of
 * the visual variants (state / worker / inline / terminal) so the
 * graph distinguishes them visually.
 *
 * Edge labels: transitions emit the predicate as the edge label when
 * non-trivial, otherwise leave the edge unlabelled.
 */

import type { Edge, Node } from '@xyflow/react';
import type { FlowNodeData, FlowNodeKind } from '../components/FlowGraph';

interface SpecStateShape {
  id: string;
  kind?: string;
  worker?: { role?: string; prompt_template?: string };
  loop?: unknown;
  inline?: { handler_id?: string };
  transitions?: SpecTransitionShape[];
}

interface SpecTransitionShape {
  to: string;
  when?: unknown;
  kind?: string;
}

interface SpecDefinitionShape {
  id?: string;
  version?: number;
  entry?: string;
  states?: SpecStateShape[];
}

function inferKind(state: SpecStateShape): FlowNodeKind {
  // Explicit `kind` always wins.
  if (state.kind === 'worker') return 'worker';
  if (state.kind === 'loop') return 'worker';
  if (state.kind === 'inline') return 'inline';
  if (state.kind === 'terminal') return 'terminal';
  // Otherwise infer from which body field is present. Body presence
  // takes precedence over the "no transitions = terminal" rule
  // because a state can have a worker/inline body AND no transitions
  // (a final worker that returns the answer; still NOT terminal in
  // the graph-rendering sense — it has work to perform).
  if (state.inline) return 'inline';
  if (state.worker || state.loop) return 'worker';
  if (!state.transitions || state.transitions.length === 0) return 'terminal';
  return 'state';
}

function predicateLabel(t: SpecTransitionShape): string {
  if (typeof t.when === 'string') return t.when;
  if (t.when && typeof t.when === 'object') {
    const obj = t.when as Record<string, unknown>;
    // The FSM library's Transition model encodes the human-readable
    // text in different keys depending on the kind: deterministic
    // puts it in `.expression`, judgement in `.criteria`, and bare
    // Predicate dumps map to `.predicate`. Check all three so the
    // visible edge label never goes blank for a transition that
    // genuinely has guard text.
    if (typeof obj.predicate === 'string') return obj.predicate;
    if (typeof obj.expression === 'string') return obj.expression;
    if (typeof obj.criteria === 'string') return obj.criteria;
  }
  return '';
}

/** Derive the transition kind (always / otherwise / deterministic /
 *  judgement) from the various `when` shapes the FSM library emits.
 *  The Transition model does NOT carry a top-level `kind` field — it
 *  lives inside `when` (or is the bare string "always"/"otherwise").
 *  Returns undefined when the shape isn't recognised.
 *
 *  Exported so other inspector surfaces (the transitions list in
 *  StateInspectorBody, etc.) can derive the kind the same way as the
 *  graph edge, instead of duplicating the logic and drifting. */
export function transitionKind(t: { when?: unknown }): string | undefined {
  if (typeof t.when === 'string') {
    if (t.when === 'always' || t.when === 'otherwise') return t.when;
    // A bare predicate-string (anything else) lifts to deterministic
    // per the Python Transition normaliser.
    return 'deterministic';
  }
  if (t.when && typeof t.when === 'object') {
    const obj = t.when as Record<string, unknown>;
    if (typeof obj.kind === 'string') return obj.kind;
    // A dict with only `.expression` is deterministic per the
    // Python Transition.normalise_when contract.
    if (typeof obj.expression === 'string') return 'deterministic';
  }
  return undefined;
}

export interface SpecGraph {
  nodes: Node<FlowNodeData>[];
  edges: Edge[];
}

/**
 * Build a FlowGraph node/edge pair from a spec.definition payload.
 *
 * Idempotent: same input → same output. Handles missing fields
 * gracefully (an absent `states` array yields an empty graph rather
 * than throwing).
 */
export function specToGraph(definition: unknown): SpecGraph {
  if (!definition || typeof definition !== 'object') {
    return { nodes: [], edges: [] };
  }
  const def = definition as SpecDefinitionShape;
  const states = Array.isArray(def.states) ? def.states : [];

  const nodes: Node<FlowNodeData>[] = states.map((s) => {
    const kind = inferKind(s);
    let sublabel = '';
    if (s.worker?.role) sublabel = `worker: ${s.worker.role}`;
    else if (s.inline?.handler_id) sublabel = `inline: ${s.inline.handler_id}`;
    else if (kind === 'terminal') sublabel = 'terminal';
    return {
      id: s.id,
      position: { x: 0, y: 0 }, // dagre fills these in
      // W21: copy the FULL spec state object onto data.state so the
      // click handler can render a State Inspector Sheet without
      // re-walking the spec. fullLabel/fullSublabel mirror the
      // visible strings; node-label tooltip falls back to label when
      // these are absent.
      data: {
        kind,
        label: s.id,
        sublabel,
        fullLabel: s.id,
        fullSublabel: sublabel || undefined,
        state: s as unknown as Record<string, unknown>,
      },
    };
  });

  // Mark the entry state visually with a sublabel hint.
  if (def.entry) {
    const entry = nodes.find((n) => n.id === def.entry);
    if (entry) {
      const newSub = entry.data.sublabel
        ? `entry · ${entry.data.sublabel}`
        : 'entry';
      entry.data = {
        ...entry.data,
        sublabel: newSub,
        fullSublabel: newSub,
      };
    }
  }

  const edges: Edge[] = [];
  for (const s of states) {
    if (!Array.isArray(s.transitions)) continue;
    for (const t of s.transitions) {
      const label = predicateLabel(t);
      edges.push({
        id: `${s.id}-${t.to}-${edges.length}`,
        source: s.id,
        target: t.to,
        label: label || undefined,
        labelStyle: { fontSize: 10 },
        // W21: carry the FULL transition metadata so the Tooltip and
        // click-Sheet have access to kind + raw predicate + source/
        // target without walking the spec twice. The kind is derived
        // from `when` (the Transition model doesn't have a top-level
        // `kind` field — it's encoded inside the `when` payload).
        data: {
          fullLabel: label || undefined,
          kind: transitionKind(t),
          transition: t as unknown as Record<string, unknown>,
          sourceId: s.id,
          targetId: t.to,
        },
      });
    }
  }

  return { nodes, edges };
}
