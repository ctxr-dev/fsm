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
  purpose?: string;
  worker?: { role?: string; prompt_template?: string };
  /** Loop config. We type the structured keys we KNOW about
   *  (`max_iterations`, `done_field`) and accept extra keys via the
   *  index signature so the type doesn't collapse to `unknown` (which
   *  is what `{ ... } | unknown` would have done — a union with
   *  `unknown` loses all structure). */
  loop?: { max_iterations?: number; done_field?: string; [k: string]: unknown };
  inline?: {
    handler_id?: string;
    purpose?: string;
    response_schema?: { schema?: { properties?: Record<string, unknown> } };
    post_validations?: unknown[];
  };
  outputs?: unknown[];
  post_validations?: unknown[];
  transitions?: SpecTransitionShape[];
  verifier?: unknown;
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

/** Build a short, info-dense sublabel from structural counts on the
 *  spec state. Used when the canonical `worker: <role>` /
 *  `inline: <handler_id>` line would mirror the main label (which is
 *  the state id) and therefore add no information. Returns '' when
 *  no structural facts are worth showing, so the caller can fall
 *  through to its next default. */
function structuralSublabel(s: SpecStateShape): string {
  const parts: string[] = [];
  const outs = Array.isArray(s.outputs) ? s.outputs.length : 0;
  const trans = Array.isArray(s.transitions) ? s.transitions.length : 0;
  if (outs > 0) parts.push(`${outs} output${outs === 1 ? '' : 's'}`);
  if (trans > 0) parts.push(`→ ${trans}`);
  if (s.verifier) parts.push('✓ verifier');
  const inlinePV = Array.isArray(s.inline?.post_validations)
    ? s.inline!.post_validations!.length
    : 0;
  const statePV = Array.isArray(s.post_validations) ? s.post_validations.length : 0;
  const pv = inlinePV + statePV;
  if (pv > 0) parts.push(`post-val ×${pv}`);
  return parts.join(' · ');
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

    // Canonical sublabel: `worker: <role>` / `inline: <handler_id>` /
    // `terminal`. Always kept on `data.fullSublabel` (and surfaced via
    // hover Tooltip + the inspector Sheet) even when the visible row
    // gets demoted in favour of a more informative line below.
    let sublabel = '';
    if (s.worker?.role) sublabel = `worker: ${s.worker.role}`;
    else if (s.inline?.handler_id) sublabel = `inline: ${s.inline.handler_id}`;
    else if (kind === 'terminal') sublabel = 'terminal';
    const canonicalSublabel = sublabel || undefined;

    // W22 redundancy guard: when the canonical line would mirror the
    // main label (skill-code-review names every handler after its
    // state, so `inline: synthesize_release_readiness` duplicates the
    // label one row up), swap the visible row for new information:
    // prefer `inline.purpose` / `state.purpose` (human-readable),
    // otherwise fall through to a structural summary built from
    // counts on the same node (`N outputs · → M · ✓ verifier ·
    // post-val ×K`). The full canonical line is preserved on
    // fullSublabel so hover + inspector show it on demand.
    const mirrorsLabel =
      (s.inline?.handler_id !== undefined && s.inline.handler_id === s.id) ||
      (s.worker?.role !== undefined && s.worker.role === s.id);
    if (mirrorsLabel) {
      const purpose = (s.inline?.purpose ?? s.purpose ?? '').trim();
      const structural = structuralSublabel(s);
      sublabel = purpose || structural || '';
    }

    return {
      id: s.id,
      position: { x: 0, y: 0 }, // dagre fills these in
      // W21: copy the FULL spec state object onto data.state so the
      // click handler can render a State Inspector Sheet without
      // re-walking the spec. fullLabel/fullSublabel mirror the
      // visible strings; the Tooltip falls back to label when
      // fullLabel is absent.
      data: {
        kind,
        label: s.id,
        sublabel,
        fullLabel: s.id,
        fullSublabel: canonicalSublabel ?? sublabel ?? undefined,
        state: s as unknown as Record<string, unknown>,
      },
    };
  });

  // Mark the entry state visually with a sublabel hint. The `entry ·`
  // prefix composes onto whatever sublabel we already chose (canonical
  // worker:/inline: line OR the structural-summary demotion from the
  // W22 mirror guard). fullSublabel composes onto the CANONICAL line
  // (not the demoted visible one) so hover + inspector still see the
  // un-demoted form — losing it here would defeat the redundancy-
  // guard's whole point.
  if (def.entry) {
    const entry = nodes.find((n) => n.id === def.entry);
    if (entry) {
      const visibleSub = entry.data.sublabel
        ? `entry · ${entry.data.sublabel}`
        : 'entry';
      const fullSub = entry.data.fullSublabel
        ? `entry · ${entry.data.fullSublabel}`
        : visibleSub;
      entry.data = {
        ...entry.data,
        sublabel: visibleSub,
        fullSublabel: fullSub,
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
