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
import type { ElkEdgeSection } from '../lib/elkLayout';

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
  /** Label centre coords stamped by FlowGraph's dagre layout (graph
   *  space). When present, FsmEdge anchors the label here instead of
   *  the geometric longest-segment midpoint — this is what stops
   *  sibling labels on a fan-out from stacking on the same midpoint
   *  band even when dagre reserved distinct slack for each. */
  dagreLabel?: { x: number; y: number };
  /** ELK-computed orthogonal polyline sections for this edge. When
   *  present, FsmEdge builds its SVG path directly from these points
   *  (with rounded 90-degree corners) instead of calling
   *  ``getSmoothStepPath``. This is the orthogonal-by-construction
   *  path the user actually sees on the default (ELK) layout engine. */
  elkSections?: ElkEdgeSection[];
  /** Label centre coords stamped by the active layout pass (ELK or
   *  dagre). When present, FsmEdge anchors the label here. Replaces
   *  ``dagreLabel`` going forward; ``dagreLabel`` is kept as a
   *  fallback for the ``?layout=dagre`` URL path. */
  layoutLabel?: { x: number; y: number };
  /** W23d 1-hop hover highlight. Set by FlowGraph while a hover is
   *  active. `isHovered` is the edge the cursor is on; `highlighted`
   *  is a 1-hop neighbour (an edge that shares an endpoint with the
   *  hovered node); `dimmed` is set on every edge that's neither the
   *  hover target nor in the highlighted set. */
  isHovered?: boolean;
  highlighted?: boolean;
  dimmed?: boolean;
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
/**
 * Build an SVG path string from an ELK orthogonal polyline. Each
 * 90-degree bend gets a quarter-circle arc of the requested radius so
 * the corner reads as a chamfered turn rather than a sharp right
 * angle. Falls back to a straight L command when consecutive segments
 * are too short for the arc (Math.min(radius, segmentLength / 2)).
 *
 * Polyline shape: [startPoint, ...bendPoints, endPoint]. ELK
 * guarantees every consecutive pair is axis-aligned (horizontal or
 * vertical) when the layout is configured with edgeRouting: ORTHOGONAL,
 * which is what makes the chamfer math valid (radius applies along the
 * incoming axis then perpendicular along the outgoing axis).
 */
export function buildOrthogonalPath(
  points: ReadonlyArray<{ x: number; y: number }>,
  radius = 6,
): string {
  if (points.length === 0) return '';
  if (points.length === 1) return `M ${points[0].x} ${points[0].y}`;

  let path = `M ${points[0].x} ${points[0].y}`;
  for (let i = 1; i < points.length; i++) {
    const prev = points[i - 1];
    const curr = points[i];
    const next = points[i + 1];
    if (next === undefined) {
      // Last segment: straight line to end.
      path += ` L ${curr.x} ${curr.y}`;
      continue;
    }
    // Distance to the corner. Clamp the chamfer radius so a short
    // segment doesn't produce a backwards arc.
    const dx1 = curr.x - prev.x;
    const dy1 = curr.y - prev.y;
    const dx2 = next.x - curr.x;
    const dy2 = next.y - curr.y;
    const len1 = Math.hypot(dx1, dy1);
    const len2 = Math.hypot(dx2, dy2);
    // Colinear (no actual bend): emit a plain L. The cross product is
    // zero when the two segments point in the same OR opposite
    // direction; either way there is no corner to round.
    const cross = dx1 * dy2 - dy1 * dx2;
    if (Math.abs(cross) < 0.5) {
      path += ` L ${curr.x} ${curr.y}`;
      continue;
    }
    const r = Math.min(radius, len1 / 2, len2 / 2);
    if (r <= 0.5) {
      path += ` L ${curr.x} ${curr.y}`;
      continue;
    }
    // Arc start: r away from curr along the incoming direction
    // (heading INTO curr from prev).
    const inUnitX = len1 > 0 ? dx1 / len1 : 0;
    const inUnitY = len1 > 0 ? dy1 / len1 : 0;
    const arcStartX = curr.x - inUnitX * r;
    const arcStartY = curr.y - inUnitY * r;
    // Arc end: r away from curr along the outgoing direction
    // (heading OUT of curr toward next).
    const outUnitX = len2 > 0 ? dx2 / len2 : 0;
    const outUnitY = len2 > 0 ? dy2 / len2 : 0;
    const arcEndX = curr.x + outUnitX * r;
    const arcEndY = curr.y + outUnitY * r;
    path += ` L ${arcStartX} ${arcStartY}`;
    // Q (quadratic Bezier) with the corner as the control point
    // produces a visually correct rounded chamfer without requiring
    // an A (arc) command. The bend's sweep direction is implied by
    // the order of the start/end points so we don't need an explicit
    // sweep flag.
    path += ` Q ${curr.x} ${curr.y} ${arcEndX} ${arcEndY}`;
  }
  return path;
}

/**
 * Convert an ElkEdgeSection list to the polyline FsmEdge will draw.
 * Each section contributes [startPoint, ...bendPoints, endPoint];
 * consecutive sections are stitched (the next section's startPoint
 * follows the previous section's endPoint).
 */
function sectionsToPolyline(
  sections: ReadonlyArray<ElkEdgeSection>,
): Array<{ x: number; y: number }> {
  const pts: Array<{ x: number; y: number }> = [];
  for (let i = 0; i < sections.length; i++) {
    const s = sections[i];
    if (i === 0) pts.push(s.startPoint);
    for (const b of s.bendPoints ?? []) pts.push(b);
    pts.push(s.endPoint);
  }
  return pts;
}

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

  const ed = (data ?? {}) as FsmEdgeData;

  // ELK path takes priority when FlowGraph stamped elkSections onto
  // edge.data — build the SVG path from the orthogonal polyline with
  // rounded 90-degree corners. This is the default rendering path on
  // ?layout=elk (the new default). When the layout fell back to dagre
  // (?layout=dagre OR an ELK throw), elkSections is absent and we use
  // xyflow's getSmoothStepPath as before.
  let edgePath: string;
  if (ed.elkSections && ed.elkSections.length > 0) {
    const polyline = sectionsToPolyline(ed.elkSections);
    edgePath = buildOrthogonalPath(polyline, 6);
  } else {
    [edgePath] = getSmoothStepPath({
      sourceX,
      sourceY,
      targetX,
      targetY,
      sourcePosition,
      targetPosition,
      borderRadius: 4,
    });
  }

  // Prefer the layout-assigned label position when FlowGraph captured
  // one for this edge. ELK's pass writes ``data.layoutLabel``; the
  // legacy dagre pass writes ``data.dagreLabel`` (kept for backwards
  // compatibility on the ?layout=dagre fallback). Fallback is the
  // geometric longest-segment midpoint: it still wins for edges added
  // by ad-hoc callers that bypass either layout (e.g. unit-test
  // fixtures).
  const layoutLabelPos = ed.layoutLabel ?? ed.dagreLabel;
  const [labelX, labelY] = layoutLabelPos
    ? [layoutLabelPos.x, layoutLabelPos.y]
    : longestSegmentMidpoint(
        sourceX,
        sourceY,
        targetX,
        targetY,
        sourcePosition,
        targetPosition,
      );
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
            class={[
              'pointer-events-auto nodrag nopan',
              // W23d 1-hop hover highlight. The edge's underlying SVG
              // path opacity is set on its `style` from FlowGraph, but
              // the label pill lives in the EdgeLabelRenderer portal
              // and needs its own dimming class. The spec calls for
              // the pill to stay at full opacity when the edge itself
              // is hovered (so the predicate text remains readable
              // while the operator is reading it). `isHovered` and
              // `highlighted` both keep full opacity; only the
              // dimmed-but-not-highlighted state fades.
              'transition-opacity duration-150 motion-reduce:transition-none',
              ed.dimmed && !ed.isHovered && !ed.highlighted ? 'opacity-30' : '',
            ].join(' ')}
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
