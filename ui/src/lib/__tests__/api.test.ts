import { describe, it, expect, vi, afterEach } from 'vitest';
import { ApiClient, ApiError, type RunSummary } from '../api';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('ApiClient.listRuns', () => {
  afterEach(() => vi.restoreAllMocks());

  it('GETs /runs and parses the response', async () => {
    const rows: RunSummary[] = [
      {
        id: 'r1', project_id: 'p', fsm_spec_id: 's', status: 'running',
        current_state: 'A', next_state: null, verdict: null,
        started_at: '2026-01-01T00:00:00Z', ended_at: null,
        last_update_at: '2026-01-01T00:00:00Z', transitions_count: 3,
      },
    ];
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(jsonResponse(rows));
    const client = new ApiClient('/api/v1');

    const result = await client.listRuns({ status: 'running', limit: 5 });

    expect(result).toEqual(rows);
    expect(fetchSpy).toHaveBeenCalledOnce();
    const [url, init] = fetchSpy.mock.calls[0];
    expect(url).toBe('/api/v1/runs?status=running&limit=5');
    expect((init as RequestInit).method).toBe('GET');
  });

  it('throws ApiError on non-2xx', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse({ detail: 'nope' }, 404),
    );
    const client = new ApiClient('/api/v1');
    await expect(client.listRuns()).rejects.toBeInstanceOf(ApiError);
  });
});
