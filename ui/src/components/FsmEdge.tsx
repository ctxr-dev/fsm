/**
 * FsmEdge — custom xyflow edge type that wraps the rendered label in
 * a Tooltip so the FULL predicate text is reachable on hover (and via
 * keyboard focus on the inner span), even when decorateEdges has
 * truncated the visible label to LABEL_MAX_CHARS to fit the dagre
 * layout reservation.
 *
 * The base edge path uses getSmoothStepPath (matches FlowGraph's
 * default 'step' edge type) so visuals are identical to PR #56. The
 * label is rendered inside xyflow's EdgeLabelRenderer (which portals
 * out of the SVG so HTML elements like our Tooltip can mount + position
 * correctly).
 *
 * Edge data contract (set by specGraph + decorateEdges):
 *   data.fullLabel?: string  — original untruncated predicate text
 *   data.kind?: string       — transition kind (always/otherwise/
 *                              deterministic/judgement)
 *   data.transition?: unknown — raw transition object for inspector
 *   data.sourceId?: string   — convenience (= edge.source)
 *   data.targetId?: string   — convenience (= edge.target)
 *
 * The edge label is BOTH a hover trigger (Tooltip) AND a click target.
 * Because clicking an HTML element inside EdgeLabelRenderer doesn't
 * fire xyflow's onEdgeClick (the SVG path is the only thing xyflow
 * delegates clicks from), we read the FlowGraph's onEdgeClick from a
 * Preact Context (instance-scoped, so two FlowGraphs mounted at once
 * dispatch to their own handlers — see W21 Copilot review #57).
 */

import { createContext } from 'preact';
import { useContext } from 'preact/hooks';
import type { JSX } from 'preact';
import {
  BaseEdge,
  EdgeLabelRenderer,
  getSmoothStepPath,
  type EdgeProps,
} from '@xyflow/react';

import { Tooltip } from './Tooltip';

/**
 * Context that carries the parent FlowGraph's `onEdgeClick` handler
 * down into every rendered FsmEdge. FlowGraph wraps its ReactFlow
 * subtree in a Provider with the current handler. Using context (not
 * a module-level registry) keeps each FlowGraph instance scoped to
 * its own click target — critical if two graphs render at once or a
 * graph unmounts/remounts in the same tree.
 */
export const FsmEdgeClickContext = createContext<
  ((id: string, data?: Record<string, unknown>) => void) | null
>(null);

export interface FsmEdgeData extends Record<string, unknown> {
  fullLabel?: string;
  kind?: string;
  transition?: unknown;
  sourceId?: string;
  targetId?: string;
}

export function FsmEdge(props: EdgeProps): JSX.Element {
  const {
    id,
    sourceX,
    sourceY,
    targetX,
    targetY,
    sourcePosition,
    targetPosition,
    label,
    labelStyle,
    style,
    markerEnd,
    data,
  } = props;
  // Per-instance click handler from the enclosing FlowGraph. Falls
  // back to null when no FlowGraph wrapped this edge (e.g. an isolated
  // unit-test render); the onClick path is a no-op in that case.
  const onLabelClick = useContext(FsmEdgeClickContext);

  const [edgePath, labelX, labelY] = getSmoothStepPath({
    sourceX,
    sourceY,
    targetX,
    targetY,
    sourcePosition,
    targetPosition,
    borderRadius: 4,
  });

  const ed = (data ?? {}) as FsmEdgeData;
  const fullText = typeof ed.fullLabel === 'string' && ed.fullLabel.length > 0
    ? ed.fullLabel
    : typeof label === 'string'
    ? label
    : '';

  const visibleLabel = label;

  // interactionWidth gives the SVG path a 20px-wide invisible click
  // zone so clicking near the edge fires xyflow's onEdgeClick without
  // requiring pixel-perfect aim on the 1.5px visible stroke. Without
  // this, the user has to land precisely on the line to open the
  // transition inspector — too unreliable for the user's stated
  // "click to see full info" contract.
  return (
    <>
      <BaseEdge
        id={id}
        path={edgePath}
        style={style}
        markerEnd={markerEnd}
        interactionWidth={20}
      />
      {visibleLabel ? (
        <EdgeLabelRenderer>
          {/* The wrapper position is set via inline style because xyflow's
              EdgeLabelRenderer mounts each label at the (labelX, labelY)
              we compute above; an external CSS class can't carry per-
              edge coordinates. nodrag/nopan stop xyflow from hijacking
              pointer events when the user hovers/clicks the label. */}
          <div
            class="absolute pointer-events-auto nodrag nopan"
            // eslint-disable-next-line react/forbid-dom-props -- per-edge position is computed from getSmoothStepPath
            style={{
              transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)`,
            }}
            data-edge-id={id}
          >
            {(() => {
              // W22: highlight predicate edges — deterministic /
              // judgement transitions carry meaningful guard text and
              // should pop visually. always / otherwise bare guards
              // keep the neutral slate badge so the eye is drawn to
              // the conditions that actually MATTER for understanding
              // why the FSM branched.
              const kind = ed.kind;
              const isPredicate = kind === 'deterministic' || kind === 'judgement';
              const badgeClass = isPredicate
                ? [
                    'inline-block max-w-[160px] truncate cursor-pointer',
                    'rounded px-1.5 py-0.5 text-[10px] leading-tight font-semibold font-mono',
                    'bg-amber-100 dark:bg-amber-900/40',
                    'border border-amber-400 dark:border-amber-600',
                    'text-amber-900 dark:text-amber-200',
                    'focus:outline-none focus-visible:ring-2 focus-visible:ring-amber-500',
                  ].join(' ')
                : [
                    'inline-block max-w-[160px] truncate cursor-pointer',
                    'rounded px-1.5 py-0.5 text-[10px] leading-tight',
                    'bg-[var(--xy-label-bg,#f8fafc)]/90',
                    'border border-slate-200 dark:border-slate-700',
                    'text-slate-700 dark:text-slate-200',
                    'focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500',
                  ].join(' ');
              return (
            <Tooltip content={fullText} delay={400}>
              <button
                type="button"
                class={badgeClass}
                // No native title= here: the custom Tooltip already
                // surfaces fullText with the right delay + ARIA wiring,
                // and a sibling native title would produce a double
                // bubble (instant + delayed) on hover.
                // eslint-disable-next-line react/forbid-dom-props -- font-size is xyflow's edge-label convention; matches the legacy decorateEdges labelStyle
                style={labelStyle as JSX.CSSProperties | undefined}
                onClick={(e) => {
                  e.stopPropagation();
                  onLabelClick?.(id, ed as Record<string, unknown>);
                }}
                aria-label={`Inspect transition: ${fullText}`}
              >
                {visibleLabel}
              </button>
            </Tooltip>
              );
            })()}
          </div>
        </EdgeLabelRenderer>
      ) : null}
    </>
  );
}

export default FsmEdge;
