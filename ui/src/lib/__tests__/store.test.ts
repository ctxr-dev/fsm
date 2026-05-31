import { describe, it, expect, beforeEach } from 'vitest';
import {
  EVENT_LOG_CAP,
  appendEvent,
  clearEventLog,
  connectionState,
  eventLog,
  runsByStatus,
  selectedRunId,
  setConnectionState,
  setRunsByStatus,
  setSelectedRunId,
} from '../store';
import type { Event as FsmEvent, RunSummary } from '../api';

function makeEvent(id: string): FsmEvent {
  return {
    id, run_id: 'r1', kind: 'state.entered', producer_id: 'p1',
    payload: {}, created_at: '2026-01-01T00:00:00Z', seq: 1,
  };
}

describe('store signals', () => {
  beforeEach(() => {
    clearEventLog();
    setSelectedRunId(null);
    setRunsByStatus({});
    setConnectionState('connecting');
  });

  it('setSelectedRunId mutates signal', () => {
    setSelectedRunId('abc');
    expect(selectedRunId.value).toBe('abc');
  });

  it('setRunsByStatus replaces the map', () => {
    const rows: RunSummary[] = [];
    setRunsByStatus({ running: rows });
    expect(runsByStatus.value).toEqual({ running: rows });
  });

  it('setConnectionState updates state', () => {
    setConnectionState('open');
    expect(connectionState.value).toBe('open');
  });

  it('appendEvent caps the log at EVENT_LOG_CAP', () => {
    for (let i = 0; i < EVENT_LOG_CAP + 5; i++) appendEvent(makeEvent(`e${i}`));
    expect(eventLog.value).toHaveLength(EVENT_LOG_CAP);
    expect(eventLog.value[eventLog.value.length - 1].id).toBe(
      `e${EVENT_LOG_CAP + 4}`,
    );
  });
});
