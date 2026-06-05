/**
 * Tests for the FsmNode + LoopNode renderers that live inside
 * FlowGraph.tsx (exported from there for direct test access).
 *
 * Focus: W23e viewport-zoom-aware detail toggle. The orchestrator
 * stamps `data.detailLevel` on every node based on the live zoom; this
 * file pins down what each renderer DRAWS for each level so the visual
 * contract is enforced even when the orchestrator's selection logic
 * changes. The compact path drops the kind chip and truncates the
 * state name; the full path keeps both.
 */

import { afterEach, describe, expect, test, vi } from 'vitest';
import { cleanup, render } from '@testing-library/preact';

// FsmNode + LoopNode never read xyflow's store directly — only the
// ZoomDetailWatcher does. We still mock the package so Handle renders
// as null and we don't pull in the real SVG layer under jsdom.
vi.mock('@xyflow/react', () => ({
  Handle: () => null,
  Position: { Left: 'left', Right: 'right', Top: 'top', Bottom: 'bottom' },
}));

import {
  FsmNode,
  LoopNode,
  COMPACT_LABEL_MAX_CHARS,
  truncateCompactLabel,
  type FlowNodeData,
} from '../FlowGraph';

afterEach(() => cleanup());

describe('truncateCompactLabel', () => {
  test('returns short labels unchanged', () => {
    expect(truncateCompactLabel('plan')).toBe('plan');
    expect(truncateCompactLabel('a'.repeat(COMPACT_LABEL_MAX_CHARS))).toBe(
      'a'.repeat(COMPACT_LABEL_MAX_CHARS),
    );
  });

  test('truncates long labels to MAX chars + ellipsis', () => {
    // The spec example: 'plan_specialist_batches' (23 chars) clips to
    // 'plan_specialis' (14) + ellipsis.
    const out = truncateCompactLabel('plan_specialist_batches');
    expect(out).toBe('plan_specialis…');
    expect(out.length).toBe(COMPACT_LABEL_MAX_CHARS + 1);
  });

  test('non-string input collapses to empty string (defensive)', () => {
    expect(truncateCompactLabel(undefined as unknown as string)).toBe('');
  });
});

describe('FsmNode render paths', () => {
  test('full mode renders the kind chip + the untruncated label', () => {
    const data: FlowNodeData = {
      kind: 'worker',
      label: 'plan_specialist_batches',
      detailLevel: 'full',
    };
    const { container, getByTestId, getByText } = render(
      (FsmNode as any)({ data, id: 't', sourcePosition: 'bottom', targetPosition: 'top' }),
    );
    // Kind chip is visible.
    expect(getByTestId('fsm-node-kind-chip').textContent).toBe('worker');
    // Full label rendered — NOT truncated.
    expect(getByText('plan_specialist_batches')).toBeInTheDocument();
    // data-detail-level reflects the active mode for downstream
    // CSS / a11y hooks + e2e tests.
    const wrapper = container.querySelector('[data-detail-level]') as HTMLElement;
    expect(wrapper.getAttribute('data-detail-level')).toBe('full');
  });

  test('compact mode hides the kind chip + truncates the label', () => {
    const data: FlowNodeData = {
      kind: 'worker',
      label: 'plan_specialist_batches',
      detailLevel: 'compact',
    };
    const { container, queryByTestId, getByTestId } = render(
      (FsmNode as any)({ data, id: 't', sourcePosition: 'bottom', targetPosition: 'top' }),
    );
    // Kind chip is absent in compact mode.
    expect(queryByTestId('fsm-node-kind-chip')).toBeNull();
    // Truncated label landed under the compact-specific testid.
    const compactLabel = getByTestId('fsm-node-label-compact');
    expect(compactLabel.textContent).toBe('plan_specialis…');
    const wrapper = container.querySelector('[data-detail-level]') as HTMLElement;
    expect(wrapper.getAttribute('data-detail-level')).toBe('compact');
  });

  test('omitted detailLevel defaults to full (back-compat baseline)', () => {
    const data: FlowNodeData = {
      kind: 'worker',
      label: 'short',
    };
    const { container, getByTestId, queryByTestId } = render(
      (FsmNode as any)({ data, id: 't', sourcePosition: 'bottom', targetPosition: 'top' }),
    );
    expect(getByTestId('fsm-node-kind-chip')).toBeInTheDocument();
    expect(queryByTestId('fsm-node-label-compact')).toBeNull();
    const wrapper = container.querySelector('[data-detail-level]') as HTMLElement;
    expect(wrapper.getAttribute('data-detail-level')).toBe('full');
  });

  test('compact mode applies the compact wrapper dimensions (100x44)', () => {
    const data: FlowNodeData = {
      kind: 'worker',
      label: 'short',
      detailLevel: 'compact',
    };
    const { container } = render((FsmNode as any)({ data, id: 't', sourcePosition: 'bottom', targetPosition: 'top' }));
    const wrapper = container.querySelector('[data-detail-level="compact"]') as HTMLElement;
    expect(wrapper.style.width).toBe('100px');
    expect(wrapper.style.minHeight).toBe('44px');
  });

  test('full mode applies the full wrapper dimensions (160x60)', () => {
    const data: FlowNodeData = {
      kind: 'worker',
      label: 'short',
      detailLevel: 'full',
    };
    const { container } = render((FsmNode as any)({ data, id: 't', sourcePosition: 'bottom', targetPosition: 'top' }));
    const wrapper = container.querySelector('[data-detail-level="full"]') as HTMLElement;
    expect(wrapper.style.width).toBe('160px');
    expect(wrapper.style.minHeight).toBe('60px');
  });
});

describe('LoopNode render paths', () => {
  test('compact mode hides the loop chip but keeps the ×N badge', () => {
    const data: FlowNodeData = {
      kind: 'loop',
      label: 'tick_per_finding',
      isLoop: true,
      loopMaxIterations: 5,
      iterationCount: 2,
      iterationEntries: [],
      detailLevel: 'compact',
    };
    const { getByTestId, queryByTestId } = render((LoopNode as any)({ data, id: 't', sourcePosition: 'bottom', targetPosition: 'top' }));
    expect(queryByTestId('loop-node-kind-chip')).toBeNull();
    const badge = getByTestId('loop-iteration-badge');
    expect(badge.textContent).toContain('2');
    expect(badge.textContent).toContain('5');
    const compactLabel = getByTestId('loop-node-label-compact');
    expect(compactLabel.textContent).toBe('tick_per_findi…');
  });

  test('full mode shows the "loop" kind chip + the full label', () => {
    const data: FlowNodeData = {
      kind: 'loop',
      label: 'tick',
      isLoop: true,
      loopMaxIterations: 5,
      iterationCount: 2,
      iterationEntries: [],
      detailLevel: 'full',
    };
    const { getByTestId, getByText } = render((LoopNode as any)({ data, id: 't', sourcePosition: 'bottom', targetPosition: 'top' }));
    expect(getByTestId('loop-node-kind-chip').textContent).toBe('loop');
    expect(getByText('tick')).toBeInTheDocument();
  });
});
