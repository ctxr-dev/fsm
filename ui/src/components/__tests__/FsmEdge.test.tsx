/**
 * Tests for components/FsmEdge.tsx.
 *
 * These exercise the two rendering branches:
 *
 *   1. ELK path (default): edge.data.elkSections is set; FsmEdge
 *      builds an orthogonal SVG path with rounded corners from the
 *      polyline. No call to getSmoothStepPath.
 *   2. Fallback path (no elkSections): FsmEdge calls
 *      getSmoothStepPath with (sourceX, sourceY, targetX, targetY) and
 *      renders that string. This matches the dagre-fallback and ad-hoc
 *      caller paths.
 */

import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { cleanup, render } from '@testing-library/preact';

const baseEdgeCalls: Array<Record<string, unknown>> = [];
const smoothStepCalls: Array<Record<string, unknown>> = [];

vi.mock('@xyflow/react', () => ({
  BaseEdge: (props: Record<string, unknown>) => {
    baseEdgeCalls.push(props);
    return <path data-testid="base-edge" d={props.path as string} />;
  },
  EdgeLabelRenderer: (p: { children?: unknown }) => <>{p.children as any}</>,
  getSmoothStepPath: (args: Record<string, unknown>) => {
    smoothStepCalls.push(args);
    return ['M0,0 L1,1 SMOOTHSTEP', 0, 0, 0, 0];
  },
  Position: { Left: 'left', Right: 'right', Top: 'top', Bottom: 'bottom' },
}));

import { FsmEdge, buildOrthogonalPath } from '../FsmEdge';
import type { ElkEdgeSection } from '../../lib/elkLayout';

beforeEach(() => {
  baseEdgeCalls.length = 0;
  smoothStepCalls.length = 0;
});
afterEach(() => cleanup());

describe('FsmEdge', () => {
  test('orthogonal path is generated from edge.data.elkSections (no getSmoothStepPath call)', () => {
    const sections: ElkEdgeSection[] = [
      {
        id: 's0',
        startPoint: { x: 0, y: 0 },
        bendPoints: [
          { x: 100, y: 0 },
          { x: 100, y: 50 },
        ],
        endPoint: { x: 200, y: 50 },
      },
    ];
    render(
      <FsmEdge
        {...({
          id: 'a-b',
          sourceX: 0,
          sourceY: 0,
          targetX: 200,
          targetY: 50,
          sourcePosition: 'bottom',
          targetPosition: 'top',
          label: 'predicate',
          data: { fullLabel: 'predicate', elkSections: sections },
        } as unknown as Parameters<typeof FsmEdge>[0])}
      />,
    );

    // Fallback branch must NOT have run.
    expect(smoothStepCalls.length).toBe(0);
    // Path was forwarded to BaseEdge.
    expect(baseEdgeCalls.length).toBe(1);
    const d = baseEdgeCalls[0].path as string;
    // Path starts with the polyline's first point.
    expect(d.startsWith('M 0 0')).toBe(true);
    // And reaches the endpoint.
    expect(d.includes('200')).toBe(true);
    expect(d.includes('50')).toBe(true);
    // And contains at least one quadratic Bezier (rounded corner) OR
    // a linear command — i.e. it is NOT the smooth-step fallback
    // string we'd have got from getSmoothStepPath.
    expect(d.includes('SMOOTHSTEP')).toBe(false);
  });

  test('falls back to getSmoothStepPath when edge.data.elkSections is absent', () => {
    render(
      <FsmEdge
        {...({
          id: 'a-b',
          sourceX: 10,
          sourceY: 20,
          targetX: 110,
          targetY: 120,
          sourcePosition: 'bottom',
          targetPosition: 'top',
          label: 'fallback',
          data: { fullLabel: 'fallback' },
        } as unknown as Parameters<typeof FsmEdge>[0])}
      />,
    );

    // Fallback branch ran with the right coordinates.
    expect(smoothStepCalls.length).toBe(1);
    expect(smoothStepCalls[0]).toMatchObject({
      sourceX: 10,
      sourceY: 20,
      targetX: 110,
      targetY: 120,
    });
    // Path forwarded to BaseEdge is the mock smooth-step string.
    expect(baseEdgeCalls[0].path).toBe('M0,0 L1,1 SMOOTHSTEP');
  });

  test('layoutLabel position takes priority over geometric midpoint when both are available', () => {
    // Pass elkSections so the ELK path runs; layoutLabel should be
    // honoured even though the polyline's geometric midpoint is at a
    // different point.
    const sections: ElkEdgeSection[] = [
      {
        id: 's0',
        startPoint: { x: 0, y: 0 },
        endPoint: { x: 200, y: 0 },
      },
    ];
    const { getByText } = render(
      <FsmEdge
        {...({
          id: 'a-b',
          sourceX: 0,
          sourceY: 0,
          targetX: 200,
          targetY: 0,
          sourcePosition: 'right',
          targetPosition: 'left',
          label: 'predicate-here',
          data: {
            fullLabel: 'predicate-here',
            elkSections: sections,
            layoutLabel: { x: 42, y: 17 },
          },
        } as unknown as Parameters<typeof FsmEdge>[0])}
      />,
    );
    // The label pill is rendered inside an absolutely-positioned div
    // whose transform places it at translate(42px, 17px).
    const pill = getByText('predicate-here').closest('div');
    expect(pill).not.toBeNull();
    const style = (pill as HTMLElement).getAttribute('style') ?? '';
    expect(style).toContain('translate(42px, 17px)');
  });
});

describe('buildOrthogonalPath', () => {
  test('empty input returns empty string', () => {
    expect(buildOrthogonalPath([])).toBe('');
  });

  test('single point returns a bare M command', () => {
    expect(buildOrthogonalPath([{ x: 5, y: 7 }])).toBe('M 5 7');
  });

  test('two points produce M then L (no corner)', () => {
    const d = buildOrthogonalPath([
      { x: 0, y: 0 },
      { x: 100, y: 0 },
    ]);
    expect(d).toBe('M 0 0 L 100 0');
  });

  test('right-angle bend produces L then Q (rounded chamfer)', () => {
    const d = buildOrthogonalPath(
      [
        { x: 0, y: 0 },
        { x: 100, y: 0 },
        { x: 100, y: 50 },
      ],
      6,
    );
    // M start, L to (100-6, 0), Q corner to (100, 6), L to endpoint.
    expect(d.startsWith('M 0 0')).toBe(true);
    expect(d).toContain(' L 94 0');
    expect(d).toContain(' Q 100 0');
    expect(d.endsWith('L 100 50')).toBe(true);
  });

  test('zero-length bends fall through to straight L', () => {
    // Three colinear points => no corner to round.
    const d = buildOrthogonalPath([
      { x: 0, y: 0 },
      { x: 50, y: 0 },
      { x: 100, y: 0 },
    ]);
    expect(d).toContain(' L 50 0');
    expect(d).toContain(' L 100 0');
    // No Q command for a degenerate bend.
    expect(d.includes(' Q ')).toBe(false);
  });
});
