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
  Position,
  type EdgeProps,
} from '@xyflow/react';

import { Tooltip } from './Tooltip';
import { tokenisePredicate, classForPredicateKind } from '../lib/predicateTokens';

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

/**
 * Compute the midpoint of the LONGEST segment of the orthogonal step
 * polyline xyflow emits between (sx, sy) and (tx, ty) given the
 * source/target handle positions.
 *
 * xyflow's getSmoothStepPath produces a path with one or two 90-degree
 * bends. For TB (source=Bottom, target=Top) the polyline is
 *   (sx, sy) -> (sx, midY) -> (tx, midY) -> (tx, ty)
 * with midY = (sy + ty) / 2. For LR (source=Right, target=Left) it is
 *   (sx, sy) -> (midX, sy) -> (midX, ty) -> (tx, ty)
 * with midX = (sx + tx) / 2.
 *
 * We walk the polyline, find the longest Manhattan-length segment, and
 * return its midpoint. This positions the label on the dominant
 * straight run rather than on the corner bend, which is what xyflow's
 * built-in labelX/labelY would otherwise pick.
 */
function longestSegmentMidpoint(
  sx: number,
  sy: number,
  tx: number,
  ty: number,
  sPos: Position | undefined,
  tPos: Position | undefined,
): [number, number] {
  const vertical =
    sPos === Position.Top || sPos === Position.Bottom ||
    tPos === Position.Top || tPos === Position.Bottom;
  const pts: Array<[number, number]> = vertical
    ? (() => {
        const mid = (sy + ty) / 2;
        return [
          [sx, sy],
          [sx, mid],
          [tx, mid],
          [tx, ty],
        ];
      })()
    : (() => {
        const mid = (sx + tx) / 2;
        return [
          [sx, sy],
          [mid, sy],
          [mid, ty],
          [tx, ty],
        ];
      })();
  let best = -1;
  let bx = pts[0][0];
  let by = pts[0][1];
  for (let i = 0; i < pts.length - 1; i++) {
    const [x1, y1] = pts[i];
    const [x2, y2] = pts[i + 1];
    const len = Math.abs(x2 - x1) + Math.abs(y2 - y1);
    if (len > best) {
      best = len;
      bx = (x1 + x2) / 2;
      by = (y1 + y2) / 2;
    }
  }
  return [bx, by];
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

  const [edgePath] = getSmoothStepPath({
    sourceX,
    sourceY,
    targetX,
    targetY,
    sourcePosition,
    targetPosition,
    borderRadius: 4,
  });

  // W23b regression fix: xyflow's getSmoothStepPath returns the
  // GEOMETRIC centre of the path, which for an L-shape lands on the
  // bend itself, a visually poor anchor that often overlaps a
  // downstream node corner. Re-derive the polyline xyflow emits for a
  // 90-degree step edge (4 points, 3 segments) and place the label at
  // the midpoint of the LONGEST segment instead. This keeps the label
  // on the dominant straight run where there is the most clearance.
  const [labelX, labelY] = longestSegmentMidpoint(
    sourceX,
    sourceY,
    targetX,
    targetY,
    sourcePosition,
    targetPosition,
  );

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
            class="pointer-events-auto nodrag nopan"
            // eslint-disable-next-line react/forbid-dom-props -- per-edge position is computed from getSmoothStepPath
            style={{
              position: 'absolute',
              // xyflow canonical recipe: pixel-translate FIRST (rightmost
              // applied first), then percentage-recenter the box around
              // (labelX, labelY). The previous "%-then-px" order centred
              // the element at the origin rather than at the computed
              // anchor point, so labels visually drifted off the line on
              // horizontal segments. See xyflow docs for EdgeLabelRenderer.
              transform: `translate(${labelX}px, ${labelY}px) translate(-50%, -50%)`,
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
              // W23b regression fix: no max-w + no truncate. Labels are
              // typically short DSL predicates; a 280px soft cap with
              // wrapping handles the rare long case without cutting
              // text mid-character. The Tooltip below still surfaces
              // the full text on hover for any edge label long enough
              // to feel cramped.
              const badgeClass = isPredicate
                ? [
                    'inline-block max-w-[280px] whitespace-normal break-words cursor-pointer text-left',
                    'rounded px-1.5 py-0.5 text-[10px] leading-tight font-semibold font-mono',
                    // Dark amber background in BOTH themes so the
                    // syntax-coloured tokens (lime / cyan / fuchsia /
                    // amber-50) stay legible. The previous light-mode
                    // amber-100 background washed light tokens out.
                    'bg-amber-900/80 dark:bg-amber-900/70',
                    'border border-amber-500 dark:border-amber-600',
                    'text-amber-50',
                    'focus:outline-none focus-visible:ring-2 focus-visible:ring-amber-500',
                  ].join(' ')
                : [
                    'inline-block max-w-[280px] whitespace-normal break-words cursor-pointer text-left',
                    'rounded px-1.5 py-0.5 text-[10px] leading-tight',
                    'bg-[var(--xy-label-bg,#f8fafc)]/90',
                    'border border-slate-200 dark:border-slate-700',
                    'text-slate-700 dark:text-slate-200',
                    'focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500',
                  ].join(' ');
              // Predicate labels get syntax-coloured token spans so
              // operators / strings / numbers / function calls jump out
              // of the amber pill. Non-predicate labels (always /
              // otherwise) stay as a plain text run.
              const visibleText = typeof visibleLabel === 'string' ? visibleLabel : '';
              const labelContent = isPredicate && visibleText.length > 0
                ? tokenisePredicate(visibleText).map((t, idx) => (
                    <span key={idx} class={classForPredicateKind(t.kind)}>{t.text}</span>
                  ))
                : visibleLabel;
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
                {labelContent}
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
