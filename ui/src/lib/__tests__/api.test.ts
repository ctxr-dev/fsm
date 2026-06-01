import { describe, it, expect, vi, afterEach } from 'vitest';
import { ApiClient, ApiError, type Page, type RunSummary } from '../api';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('ApiClient.listRuns', () => {
  afterEach(() => vi.restoreAllMocks());

  it('GETs /runs with page params and parses the Page<T> envelope', async () => {
    const envelope: Page<RunSummary> = {
      items: [
        {
          id: 'r1', project_id: 'p', fsm_spec_id: 's', status: 'running',
          current_state: 'A', next_state: null, verdict: null,
          started_at: '2026-01-01T00:00:00Z', ended_at: null,
          last_update_at: '2026-01-01T00:00:00Z', transitions_count: 3,
        },
      ],
      page: 2,
      page_size: 5,
      total: 7,
      has_next: false,
      sort: 'last_update_at_desc',
    };
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(jsonResponse(envelope));
    const client = new ApiClient('/api/v1');

    const result = await client.listRuns({
      status: 'running',
      page: 2,
      page_size: 5,
      sort: 'last_update_at_desc',
    });

    expect(result).toEqual(envelope);
    expect(result.items[0].id).toBe('r1');
    expect(result.total).toBe(7);
    expect(fetchSpy).toHaveBeenCalledOnce();
    const [url, init] = fetchSpy.mock.calls[0];
    expect(url).toBe(
      '/api/v1/runs?status=running&page=2&page_size=5&sort=last_update_at_desc',
    );
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
