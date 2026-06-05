/**
 * Run-progress graph builder.
 *
 * Takes the spec graph (``specToGraph`` output) and overlays a run's
 * traversal: which states were entered, which are still entered,
 * which faulted, which is the current state, and which transitions
 * were actually taken (vs. the merely-possible edges the spec
 * declares). The result is a ``{nodes, edges}`` pair that can be
 * handed directly to :class:`FlowGraph` for rendering.
 *
 * Why a separate builder rather than parameterising ``specToGraph``?
 * The spec graph is static (one per registered spec); the run
 * overlay is per-instance and changes on every event. Keeping the
 * two layers separated means a future feature (compare two runs of
 * the same spec) can reuse one spec graph + two overlays without
 * recomputing the static layer.
 */

import type { Edge, Node } from '@xyflow/react';

import type { FlowNodeData } from '../components/FlowGraph';
import type { Event as FsmEvent, RunManifest, StateNode } from './api';

/**
 * Per-state runtime status used to colour the node + decide the badge.
 *
 * - ``not_visited``: the spec declares this state but the run never
 *   entered it. Default greyed-out rendering.
 * - ``entered``: the run is CURRENTLY in this state (no ``exited_at``
 *   stamped yet). Pulses to attract the eye.
 * - ``exited``: the run was in this state and has transitioned out
 *   successfully. Solid emerald.
 * - ``faulted``: the engine recorded a fault while this state was
 *   active. Solid red.
 */
export type RunNodeStatus = 'not_visited' | 'entered' | 'exited' | 'faulted';

/**
 * Per-iteration chip payload surfaced inside a loop node card. Each
 * entry maps 1-to-1 to a row in the state-entry tree; clicking the
 * chip opens the per-iteration StateEntrySheetBody (Tab 1 = run
 * values for THIS entry's inputs/outputs, Tab 3 = events filtered to
 * THIS entry_id) — the same per-iteration semantics PR 4 already
 * baked into the sheet body.
 */
export interface LoopIterationEntry {
  entry_id: string;
  /** 1-based iteration counter as the engine recorded it. ``null`` when
   *  the engine didn't stamp one (a defensive fallback so the chip
   *  strip still renders for entries that pre-date the counter). */
  iteration_n: number | null;
  status: string;
}

export interface RunOverlay {
  /** Map of spec-state-id -> the strongest status observed across all
   *  entries for that state. "Strongest" means a faulted entry wins
   *  over an exited entry which wins over an entered entry. */
  statusByStateId: Map<string, RunNodeStatus>;
  /** Spec-state-id the run is currently sitting on (from the manifest). */
  currentStateId: string | null;
  /** Set of ``"<from>::<to>"`` transition keys actually traversed.
   *  Derived from the chronological state-entry sequence rather than
   *  ``transitions`` events, because the state-tree gives a clean
   *  parent->child traversal without us having to filter for the
   *  transition-taken event subset. */
  takenTransitions: Set<string>;
  /** Spec-state-id where the engine recorded a fault, if any. */
  faultedStateId: string | null;
  /** PR 5: spec-state-id -> chronological list of iteration entries
   *  recorded against THAT state. The list is non-empty only for
   *  states the run actually entered (and is shaped per ``LoopIterationEntry``
   *  whether or not the spec declares ``kind: "loop"`` — the run-graph
   *  layer doesn't know spec metadata; ``overlayRunOnSpecGraph`` joins
   *  the chip strip onto loop nodes specifically). */
  iterationEntriesByStateId: Map<string, LoopIterationEntry[]>;
}

/** Flatten the state tree into a CHRONOLOGICAL list.
 *
 *  StateNode already carries ``entry_seq`` (the engine's monotonic
 *  per-run counter, the same field the rest of the UI uses to order
 *  events). We walk the tree to collect every node then sort by
 *  ``entry_seq`` ascending — this is correct for branching trees
 *  (where a single state entry can have multiple children written in
 *  whatever order the engine produced them) and idempotent regardless
 *  of how the parent traversed children. DFS pre-order, which the
 *  pre-fix draft used, only matched entry order when the tree was a
 *  linear chain.
 */
function flattenEntries(root: StateNode | null): StateNode[] {
  if (!root) return [];
  const out: StateNode[] = [];
  const walk = (node: StateNode): void => {
    out.push(node);
    for (const child of node.children) walk(child);
  };
  walk(root);
  out.sort((a, b) => a.entry_seq - b.entry_seq);
  return out;
}

/** Walk the tree and emit every parent -> child pair as a transition.
 *
 *  The state tree's parent/child relation IS the transition graph
 *  the engine took: an entry's children are precisely the states it
 *  transitioned into. This is more accurate than the
 *  adjacency-on-the-flattened-list approach, which conflated
 *  branches at a fork point (two children of the same parent) with
 *  an actual sibling-to-sibling transition that never happened.
 */
function emitTreeTransitions(root: StateNode | null, out: Set<string>): void {
  if (!root) return;
  for (const child of root.children) {
    if (root.state_id !== child.state_id) {
      out.add(`${root.state_id}::${child.state_id}`);
    }
    emitTreeTransitions(child, out);
  }
}

/** Promote ``a`` over ``b`` per the status hierarchy. */
function strongerStatus(a: RunNodeStatus, b: RunNodeStatus): RunNodeStatus {
  const order: RunNodeStatus[] = ['not_visited', 'exited', 'entered', 'faulted'];
  return order.indexOf(a) >= order.indexOf(b) ? a : b;
}

/**
 * Build the per-state-id overlay from the manifest + state tree.
 *
 * Walks the entry list in chronological order, recording the
 * STRONGEST status seen per state_id. A state that was entered
 * twice (loop iteration) and exited cleanly the first time but
 * faulted the second time ends up flagged ``faulted``.
 */
export function buildRunOverlay(
  manifest: RunManifest | null,
  stateTree: StateNode | null,
  events: FsmEvent[],
): RunOverlay {
  const statusByStateId = new Map<string, RunNodeStatus>();
  const iterationEntriesByStateId = new Map<string, LoopIterationEntry[]>();
  const entries = flattenEntries(stateTree);
  let faultedStateId: string | null = null;

  for (const entry of entries) {
    const stateId = entry.state_id;
    const prev = statusByStateId.get(stateId) ?? 'not_visited';
    // PR 5: collect every entry per state_id in chronological order
    // (flattenEntries already sorted by entry_seq). The downstream loop
    // node renderer uses this to draw a chip per iteration; non-loop
    // states still get a list here but ``overlayRunOnSpecGraph`` only
    // joins it onto nodes whose spec data carries ``isLoop=true``.
    const bucket = iterationEntriesByStateId.get(stateId) ?? [];
    bucket.push({
      entry_id: entry.entry_id,
      iteration_n: entry.iteration_n,
      status: entry.status,
    });
    iterationEntriesByStateId.set(stateId, bucket);
    // Derive entered/exited from ``exited_at`` (the timestamp is the
    // engine's source of truth: null = still active; non-null = the
    // engine has moved on). Only the ``faulted`` status string is
    // treated as a special case because the engine flags it
    // explicitly and a faulted entry typically also carries a
    // non-null exited_at. The earlier "fall back to entered on
    // unknown values" shortcut misclassified any unrecognised /
    // future status string as still-active (Copilot review on
    // PR #62).
    const status: RunNodeStatus =
      entry.status === 'faulted'
        ? 'faulted'
        : entry.exited_at === null
        ? 'entered'
        : 'exited';
    if (status === 'faulted' && faultedStateId === null) {
      faultedStateId = stateId;
    }
    statusByStateId.set(stateId, strongerStatus(prev, status));
  }

  // Compute taken transitions from the state tree's parent/child
  // relation — every (parent.state_id, child.state_id) edge in the
  // tree is by definition a transition the engine took. The pre-fix
  // adjacency-on-the-flattened-list approach was wrong for branching
  // trees: two children of the same parent are not adjacent
  // transitions of each other, they're two outbound transitions from
  // the same fork point.
  const takenTransitions = new Set<string>();
  emitTreeTransitions(stateTree, takenTransitions);
  // ``events`` is reserved for a future refinement where the
  // ``transition_taken`` event would carry an explicit
  // ``from``/``to`` pair (the most accurate signal for loops that
  // revisit a state via a non-tree edge). Today the engine doesn't
  // surface those fields on the event, so we use the tree-derived
  // adjacency. Leaving the parameter in the signature keeps the call
  // site stable when that wire-format upgrade lands.
  void events;

  return {
    statusByStateId,
    currentStateId: manifest?.current_state ?? null,
    takenTransitions,
    faultedStateId,
    iterationEntriesByStateId,
  };
}

/**
 * Per-status edge stroke colours. Node colours are owned by FsmNode
 * (which switches Tailwind classes off ``data.runStatus`` /
 * ``data.isCurrent``); this single source for the taken/untaken edge
 * stroke means we don't have to duplicate the Tailwind class map.
 */
const TAKEN_EDGE_STROKE = '#10b981';
const UNTAKEN_EDGE_STROKE = '#cbd5e1';

/**
 * Apply the overlay to the base spec graph and return a new
 * ``{nodes, edges}`` pair suitable for handing straight to FlowGraph.
 *
 * The returned arrays are new objects (no aliasing of the input) so
 * the caller can store both layers independently without diff-spotting
 * mutation bugs.
 */
export function overlayRunOnSpecGraph(
  base: { nodes: Node<FlowNodeData>[]; edges: Edge[] },
  overlay: RunOverlay,
): { nodes: Node<FlowNodeData>[]; edges: Edge[] } {
  const nodes = base.nodes.map((node) => {
    const stateId = node.id;
    const baseStatus = overlay.statusByStateId.get(stateId) ?? 'not_visited';
    // The current state always wins the visual treatment regardless
    // of its recorded ``status`` (which will usually be "entered").
    const status: RunNodeStatus =
      overlay.currentStateId === stateId && baseStatus !== 'faulted'
        ? 'entered'
        : baseStatus;
    const isCurrent = overlay.currentStateId === stateId;
    // PR 5: graft the per-iteration entries onto loop nodes. Non-loop
    // nodes ignore the field (FlowGraph dispatches the loop renderer
    // off ``data.isLoop``); we still copy the bucket so a future
    // worker-node "iteration trail" affordance can opt in without
    // changing the overlay surface again.
    const iterationEntries =
      overlay.iterationEntriesByStateId.get(stateId) ?? [];
    const iterationCount = iterationEntries.length;
    // Clean-slate rebuild: loop cards are uniform-size; no per-iteration
    // width adjustment. The chip strip lives in the inspector Sheet, so
    // an N-iteration loop renders the same card as a 1-iteration one
    // (just with a different ×N badge in the top-right corner).

    // Compose the per-status badge prefix INTO ``data.label`` rather
    // than into a parallel ``labelPrefix`` field. FlowGraph renders
    // ``data.label`` verbatim and has no labelPrefix concept, so the
    // pre-fix draft's prefix never reached the DOM. The badge needs
    // to land in the actual label string for the colour-coded view
    // to also read correctly under reduced-motion / desaturated
    // dark-mode tweaks where the background tint may be subtle.
    const badge =
      status === 'faulted'
        ? '⚠ '
        : status === 'entered'
        ? '▸ '
        : status === 'exited'
        ? '✓ '
        : '';
    // Preserve the original spec label unmodified so re-applying the
    // overlay multiple times doesn't stack prefixes (idempotent).
    const originalLabel =
      typeof node.data.label === 'string' ? node.data.label : '';
    const decoratedLabel = badge ? `${badge}${originalLabel}` : originalLabel;
    // Status is conveyed via ``data.runStatus`` / ``data.isCurrent``
    // — FsmNode (in FlowGraph.tsx) switches its Tailwind palette off
    // them. We deliberately do NOT set ``node.style`` here: FsmNode
    // renders its own padded/rounded card inside the React Flow node
    // wrapper and ignores wrapper-level inline styles, so the earlier
    // border/background/boxShadow on node.style produced no visible
    // overlay (Copilot review on PR #62).
    return {
      ...node,
      // Loop nodes use the same uniform dimensions as every other node
      // (FlowGraph stamps NODE_WIDTH/NODE_HEIGHT during the layout
      // pass), so we don't touch width/height here.
      data: {
        ...node.data,
        label: decoratedLabel,
        runStatus: status,
        isCurrent,
        iterationCount,
        iterationEntries,
      } as FlowNodeData & {
        runStatus?: RunNodeStatus;
        isCurrent?: boolean;
        iterationCount?: number;
        iterationEntries?: LoopIterationEntry[];
      },
    };
  });

  const edges = base.edges.map((edge) => {
    const taken = overlay.takenTransitions.has(`${edge.source}::${edge.target}`);
    return {
      ...edge,
      animated: taken && overlay.currentStateId === edge.source,
      style: {
        ...(edge.style ?? {}),
        stroke: taken ? TAKEN_EDGE_STROKE : UNTAKEN_EDGE_STROKE,
        strokeWidth: taken ? 2.2 : 1.4,
        opacity: taken ? 1 : 0.55,
      },
      labelStyle: {
        ...((edge.labelStyle as object) ?? {}),
        fontWeight: taken ? 600 : 400,
      },
    };
  });

  return { nodes, edges };
}

/**
 * Count visited / total states from the overlay. Used by the
 * RunProgressGraph header strip to render "13 / 15 states visited"
 * without the caller having to re-derive.
 */
export function overlayProgress(
  overlay: RunOverlay,
  totalStates: number,
): { visited: number; faulted: number; total: number } {
  let visited = 0;
  let faulted = 0;
  for (const status of overlay.statusByStateId.values()) {
    if (status !== 'not_visited') visited += 1;
    if (status === 'faulted') faulted += 1;
  }
  return { visited, faulted, total: totalStates };
}
