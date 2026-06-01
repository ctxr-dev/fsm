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
 * module-level callback registry set by FlowGraph each render and
 * invoke it ourselves on label click.
 */

import type { JSX } from 'preact';
import {
  BaseEdge,
  EdgeLabelRenderer,
  getSmoothStepPath,
  type EdgeProps,
} from '@xyflow/react';

import { Tooltip } from './Tooltip';

// Module-level callback registry. FlowGraph stamps the current
// onEdgeClick handler here before each render; FsmEdge reads it on
// click. This avoids a Preact Context for a single value, which would
// otherwise require wrapping the whole ReactFlow subtree in a Provider.
let edgeClickHandler: ((id: string, data?: Record<string, unknown>) => void) | null = null;

/**
 * FlowGraph calls this once per render to publish the current
 * onEdgeClick callback so the rendered FsmEdge instances can dispatch
 * to it from their label click handler.
 */
export function setEdgeClickHandler(
  handler: ((id: string, data?: Record<string, unknown>) => void) | null,
): void {
  edgeClickHandler = handler;
}

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
            <Tooltip content={fullText} delay={400}>
              <button
                type="button"
                class={[
                  'inline-block max-w-[160px] truncate cursor-pointer',
                  'rounded px-1.5 py-0.5 text-[10px] leading-tight',
                  'bg-[var(--xy-label-bg,#f8fafc)]/90',
                  'border border-slate-200 dark:border-slate-700',
                  'text-slate-700 dark:text-slate-200',
                  'focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500',
                ].join(' ')}
                title={fullText}
                // eslint-disable-next-line react/forbid-dom-props -- font-size is xyflow's edge-label convention; matches the legacy decorateEdges labelStyle
                style={labelStyle as JSX.CSSProperties | undefined}
                onClick={(e) => {
                  e.stopPropagation();
                  // Dispatch through the registered FlowGraph onEdgeClick.
                  edgeClickHandler?.(id, ed as Record<string, unknown>);
                }}
                aria-label={`Inspect transition: ${fullText}`}
              >
                {visibleLabel}
              </button>
            </Tooltip>
          </div>
        </EdgeLabelRenderer>
      ) : null}
    </>
  );
}

export default FsmEdge;
