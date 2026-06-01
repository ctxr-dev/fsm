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
}

/** Flatten the state tree into a chronological list (DFS pre-order
 *  matches engine entry order because the tree is built by appending
 *  children at the bottom). */
function flattenEntries(root: StateNode | null): StateNode[] {
  if (!root) return [];
  const out: StateNode[] = [];
  const walk = (node: StateNode): void => {
    out.push(node);
    for (const child of node.children) walk(child);
  };
  walk(root);
  return out;
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
  const entries = flattenEntries(stateTree);
  let faultedStateId: string | null = null;

  for (const entry of entries) {
    const stateId = entry.state_id;
    const prev = statusByStateId.get(stateId) ?? 'not_visited';
    // StateNode.status is one of "entered" / "exited" / "faulted"
    // (StateStatus enum). Fall back to "entered" on unknown values so
    // the visualisation still renders something.
    const status: RunNodeStatus =
      entry.status === 'exited'
        ? 'exited'
        : entry.status === 'faulted'
        ? 'faulted'
        : 'entered';
    if (status === 'faulted' && faultedStateId === null) {
      faultedStateId = stateId;
    }
    statusByStateId.set(stateId, strongerStatus(prev, status));
  }

  // Compute taken transitions by walking the chronological entry list
  // and recording each parent -> child pair. The engine inserts a new
  // entry whenever a state is entered, so adjacent entries in DFS
  // pre-order correspond to actually-taken transitions in the run's
  // history.
  const takenTransitions = new Set<string>();
  for (let i = 0; i < entries.length - 1; i++) {
    const from = entries[i].state_id;
    const to = entries[i + 1].state_id;
    if (from !== to) takenTransitions.add(`${from}::${to}`);
  }
  // ``events`` is reserved for a future refinement where the
  // ``transition_taken`` event would carry an explicit
  // ``from``/``to`` pair (more accurate than DFS adjacency for loops
  // that revisit a state). Today the engine doesn't surface those
  // fields on the event, so we use the state-tree adjacency. Leaving
  // the parameter here keeps the call site stable when that lands.
  void events;

  return {
    statusByStateId,
    currentStateId: manifest?.current_state ?? null,
    takenTransitions,
    faultedStateId,
  };
}

/** Per-status visual treatment (border + background tints). */
const STATUS_COLOURS: Record<RunNodeStatus, { border: string; bg: string }> = {
  not_visited: {
    border: '#94a3b8',
    bg: 'rgba(241, 245, 249, 0.6)',
  },
  entered: {
    border: '#f59e0b',
    bg: 'rgba(254, 243, 199, 0.85)',
  },
  exited: {
    border: '#10b981',
    bg: 'rgba(209, 250, 229, 0.85)',
  },
  faulted: {
    border: '#ef4444',
    bg: 'rgba(254, 202, 202, 0.9)',
  },
};

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
    const colours = STATUS_COLOURS[status];
    const isCurrent = overlay.currentStateId === stateId;

    return {
      ...node,
      data: {
        ...node.data,
        // Decorate the node label with a per-status badge prefix so
        // the colour-coded view also reads correctly under reduced-
        // motion / dark-mode mode tweaks where the background may
        // be desaturated.
        labelPrefix:
          status === 'faulted'
            ? '⚠ '
            : status === 'entered'
            ? '▸ '
            : status === 'exited'
            ? '✓ '
            : '',
        runStatus: status,
        isCurrent,
      } as FlowNodeData & {
        labelPrefix?: string;
        runStatus?: RunNodeStatus;
        isCurrent?: boolean;
      },
      style: {
        ...(node.style ?? {}),
        border: `2px solid ${colours.border}`,
        background: colours.bg,
        boxShadow: isCurrent
          ? `0 0 0 4px ${colours.border}33, 0 8px 16px rgba(0,0,0,0.10)`
          : '0 2px 6px rgba(0,0,0,0.06)',
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
        stroke: taken ? '#10b981' : '#cbd5e1',
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
