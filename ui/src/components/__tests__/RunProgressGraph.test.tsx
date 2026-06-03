/**
 * Tests for components/RunProgressGraph.tsx — PR 4 additions.
 *
 * Coverage:
 *   - onNodeClick fires with the spec-state-id (== FlowGraph node id)
 *     when the user clicks a node on the run graph.
 *   - The current state (manifest.current_state) renders with the
 *     ``fsm-pulse-current`` class so the pulse animation engages.
 */

import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { cleanup, render, waitFor } from '@testing-library/preact';

// Mock @xyflow/react the same way FlowGraph.test does so jsdom doesn't
// need to layout a real React Flow surface. We capture the props the
// FlowGraph forwards to ReactFlow so the test can drive the
// onNodeClick / onEdgeClick handlers synthetically AND inspect the
// node classes the FsmNode renderer would receive via decoratedNodes.
let lastReactFlowProps: Record<string, unknown> | null = null;
vi.mock('@xyflow/react', () => ({
  ReactFlow: (props: Record<string, unknown> & { children?: unknown; nodeTypes?: Record<string, unknown> }) => {
    lastReactFlowProps = props;
    // Render each node via its FsmNode type so the test can assert the
    // amber pulse class lands on the current node's DOM.
    const Fsm = (props.nodeTypes?.fsmNode ?? null) as
      | ((p: { data: Record<string, unknown>; selected?: boolean }) => any)
      | null;
    const nodes = (props.nodes as Array<{
      id: string;
      data: Record<string, unknown>;
      selected?: boolean;
    }>) ?? [];
    return (
      <div data-testid="mock-react-flow">
        {Fsm
          ? nodes.map((n) => (
              <div key={n.id} data-node-id={n.id}>
                {Fsm({ data: n.data, selected: n.selected })}
              </div>
            ))
          : null}
        {props.children as any}
      </div>
    );
  },
  Background: () => <div data-testid="bg" />,
  Controls: () => <div data-testid="controls" />,
  MiniMap: () => <div data-testid="minimap" />,
  Handle: () => null,
  BaseEdge: () => null,
  EdgeLabelRenderer: (p: { children?: unknown }) => <>{p.children as any}</>,
  getSmoothStepPath: () => ['M0,0 L1,1', 0, 0, 0, 0],
  Position: { Left: 'left', Right: 'right', Top: 'top', Bottom: 'bottom' },
  BackgroundVariant: { Dots: 'dots', Lines: 'lines', Cross: 'cross' },
  MarkerType: { ArrowClosed: 'arrowclosed', Arrow: 'arrow' },
  PanOnScrollMode: { Free: 'free', Vertical: 'vertical', Horizontal: 'horizontal' },
}));
vi.mock('@xyflow/react/dist/style.css', () => ({}));

// Mock the api so RunProgressGraph's spec fetch resolves with a known
// shape without touching the network. The minimal spec contains TWO
// states (plan + execute) so the overlay has a node to mark current.
vi.mock('../../lib/api', async () => {
  class ApiErrorMock extends Error {}
  return {
    ApiError: ApiErrorMock,
    api: {
      getSpec: vi.fn(async () => ({
        id: 'spec-1',
        project_id: 'p',
        project_slug: 'p',
        slug: 'demo',
        version: 1,
        hash: 'h',
        registered_at: '2025-01-01T00:00:00Z',
        definition: {
          entry: 'plan',
          states: [
            { id: 'plan', kind: 'worker' },
            { id: 'execute', kind: 'state' },
          ],
        },
      })),
    },
  };
});

import { RunProgressGraph } from '../RunProgressGraph';
import type { RunManifest, StateNode } from '../../lib/api';

const MANIFEST: RunManifest = {
  id: 'R',
  project_id: 'P',
  fsm_spec_id: 'spec-1',
  fsm_spec_hash: 'h',
  status: 'running',
  current_state: 'plan',
  next_state: null,
  verdict: null,
  started_at: '2025-01-01T00:00:00Z',
  ended_at: null,
  last_update_at: '2025-01-01T00:00:00Z',
  paused_at: null,
  pause_reason: null,
  parent_run_id: null,
  resume_history: [],
  args: {},
  metadata: {},
  transitions_count: 0,
};

const STATE_TREE: StateNode = {
  entry_id: 'entry-1',
  state_id: 'plan',
  entry_seq: 1,
  entered_at: '2025-01-01T00:00:00Z',
  exited_at: null,
  status: 'entered',
  inputs: {},
  outputs: {},
  iteration_n: null,
  children: [],
};

beforeEach(() => {
  lastReactFlowProps = null;
});
afterEach(() => cleanup());

describe('RunProgressGraph — PR 4 additions', () => {
  test('onNodeClick fires with the node id when ReactFlow dispatches a click', async () => {
    const onNodeClick = vi.fn();
    render(
      <RunProgressGraph
        manifest={MANIFEST}
        stateTree={STATE_TREE}
        events={[]}
        onNodeClick={onNodeClick}
      />,
    );
    // Wait until the spec fetch resolves and the FlowGraph mock has
    // received its props (the component renders a Spinner until then).
    await waitFor(() => {
      expect(lastReactFlowProps).not.toBeNull();
      expect(
        (lastReactFlowProps!.nodes as Array<{ id: string }>).length,
      ).toBeGreaterThan(0);
    });
    const rfOnNodeClick = lastReactFlowProps!.onNodeClick as (
      e: unknown,
      node: { id: string; data: Record<string, unknown> },
    ) => void;
    rfOnNodeClick({}, { id: 'plan', data: { kind: 'worker', label: 'plan' } });
    expect(onNodeClick).toHaveBeenCalledWith('plan');
  });

  test('onEdgeClick fires with (fromId, toId) extracted from edge.data', async () => {
    const onEdgeClick = vi.fn();
    render(
      <RunProgressGraph
        manifest={MANIFEST}
        stateTree={STATE_TREE}
        events={[]}
        onEdgeClick={onEdgeClick}
      />,
    );
    await waitFor(() => {
      expect(lastReactFlowProps).not.toBeNull();
    });
    const rfOnEdgeClick = lastReactFlowProps!.onEdgeClick as (
      e: unknown,
      edge: { id: string; data?: Record<string, unknown> },
    ) => void;
    rfOnEdgeClick(
      {},
      {
        id: 'plan->execute',
        data: { sourceId: 'plan', targetId: 'execute' },
      },
    );
    expect(onEdgeClick).toHaveBeenCalledWith('plan', 'execute');
  });

  test('the current state node renders with the fsm-pulse-current animation class', async () => {
    const { container } = render(
      <RunProgressGraph
        manifest={MANIFEST}
        stateTree={STATE_TREE}
        events={[]}
      />,
    );
    await waitFor(() => {
      expect(lastReactFlowProps).not.toBeNull();
    });
    // The current node is the one whose id matches manifest.current_state.
    // The mock ReactFlow wraps each node renderer in a div tagged
    // data-node-id so the test can find the node card via DOM query.
    const currentWrapper = container.querySelector('[data-node-id="plan"]');
    expect(currentWrapper).not.toBeNull();
    // The fsm-pulse-current class lives on the inner fsm-node card.
    const card = currentWrapper!.querySelector('.fsm-node');
    expect(card).not.toBeNull();
    expect(card!.className).toContain('fsm-pulse-current');
    // A non-current node must NOT carry the pulse class.
    const otherWrapper = container.querySelector('[data-node-id="execute"]');
    expect(otherWrapper).not.toBeNull();
    const otherCard = otherWrapper!.querySelector('.fsm-node');
    expect(otherCard).not.toBeNull();
    expect(otherCard!.className).not.toContain('fsm-pulse-current');
  });
});
