import { describe, it, expect, vi, afterEach } from 'vitest';
import {
  ApiClient,
  ApiError,
  ApiResponseShapeError,
  isPageEnvelope,
  walkAllPages,
  type Page,
  type RunSummary,
} from '../api';

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

// W23a Layer 2: defensive content-type + JSON-parse guards on every
// 2xx response. The pre-W23a code path silently coerced an HTML body
// into a string and handed it back as the response payload, which
// crashed downstream consumers with the reported "env.items is not
// iterable" TypeError. These tests pin the new behaviour: a 2xx
// response with the wrong content-type, or a 2xx response whose body
// fails to parse as JSON, becomes a typed `ApiResponseShapeError` with
// a recovery hint the route-level UX can act on.
describe('ApiClient.request — content-type + JSON-parse guards (W23a)', () => {
  afterEach(() => vi.restoreAllMocks());

  it('throws ApiResponseShapeError when a 2xx body has text/html (Vite SPA fallback)', async () => {
    // ``Response`` bodies are single-shot — they can be consumed only once.
    // Use ``mockImplementation`` so each call gets a fresh Response,
    // letting us assert both ``rejects`` and the typed error payload.
    vi.spyOn(globalThis, 'fetch').mockImplementation(() =>
      Promise.resolve(
        new Response('<!doctype html><html><body>not the api</body></html>', {
          status: 200,
          headers: { 'Content-Type': 'text/html; charset=utf-8' },
        }),
      ),
    );
    const client = new ApiClient('/api/v1');
    await expect(client.listRuns()).rejects.toBeInstanceOf(ApiResponseShapeError);
    try {
      await client.listRuns();
    } catch (err) {
      // hint contains the actionable advice; received carries the raw body for diagnostics.
      expect(err).toBeInstanceOf(ApiResponseShapeError);
      const e = err as ApiResponseShapeError;
      expect(e.hint).toContain('SPA shell');
      expect(typeof e.received).toBe('string');
      expect(String(e.received)).toContain('<!doctype html>');
    }
  });

  it('throws ApiResponseShapeError when content-type is missing but body is non-JSON', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response('plaintext payload', { status: 200 }),
    );
    const client = new ApiClient('/api/v1');
    await expect(client.listRuns()).rejects.toBeInstanceOf(ApiResponseShapeError);
  });

  it('throws ApiResponseShapeError when content-type is JSON but body fails to parse', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response('not-json-{[', {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    const client = new ApiClient('/api/v1');
    await expect(client.listRuns()).rejects.toBeInstanceOf(ApiResponseShapeError);
  });

  it('returns undefined for 204 No Content without invoking the shape guards', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(null, { status: 204 }),
    );
    const client = new ApiClient('/api/v1');
    // 204 returns undefined typed as T — the abortRun endpoint shape uses this.
    const result = await client.abortRun('00000000-0000-0000-0000-000000000000');
    expect(result).toBeUndefined();
  });

  it('accepts application/json with valid body (happy path unchanged)', async () => {
    const envelope: Page<RunSummary> = {
      items: [],
      page: 1,
      page_size: 50,
      total: 0,
      has_next: false,
      sort: 'last_update_at_desc',
    };
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse(envelope));
    const client = new ApiClient('/api/v1');
    const result = await client.listRuns();
    expect(result.items).toEqual([]);
    expect(result.total).toBe(0);
  });

  it('accepts vendor-specific +json content-types (e.g. application/vnd.api+json)', async () => {
    const envelope: Page<RunSummary> = {
      items: [],
      page: 1,
      page_size: 50,
      total: 0,
      has_next: false,
      sort: 'last_update_at_desc',
    };
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify(envelope), {
        status: 200,
        headers: { 'Content-Type': 'application/vnd.api+json' },
      }),
    );
    const client = new ApiClient('/api/v1');
    const result = await client.listRuns();
    expect(result.items).toEqual([]);
  });
});

// W23a Layer 1: defensive envelope-shape check inside walkAllPages.
// Even if Layer 2 lets a malformed payload through (e.g. valid JSON
// that doesn't match Page<T>), the helper must surface a typed error
// rather than crash with TypeError on `out.push(...env.items)`.
describe('walkAllPages — envelope-shape guard (W23a)', () => {
  afterEach(() => vi.restoreAllMocks());

  it('throws ApiResponseShapeError when fetcher resolves to a non-envelope (raw array)', async () => {
    const fetcher = vi.fn().mockResolvedValue([] as unknown as Page<RunSummary>);
    await expect(walkAllPages(fetcher, {})).rejects.toBeInstanceOf(ApiResponseShapeError);
  });

  it('throws ApiResponseShapeError when fetcher resolves to a Page-shaped object missing items', async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValue({ page: 1, page_size: 50, total: 0, has_next: false, sort: 'x' } as unknown as Page<RunSummary>);
    await expect(walkAllPages(fetcher, {})).rejects.toBeInstanceOf(ApiResponseShapeError);
  });

  it('throws ApiResponseShapeError when fetcher resolves to a Page-shaped object missing has_next', async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValue({ items: [], page: 1, page_size: 50, total: 0, sort: 'x' } as unknown as Page<RunSummary>);
    await expect(walkAllPages(fetcher, {})).rejects.toBeInstanceOf(ApiResponseShapeError);
  });

  it('walks pages happily when every fetch returns a valid envelope', async () => {
    const row = (i: number): RunSummary => ({
      id: `r${i}`, project_id: 'p', fsm_spec_id: 's', status: 'completed',
      current_state: 'A', next_state: null, verdict: null,
      started_at: '2026-01-01T00:00:00Z', ended_at: null,
      last_update_at: '2026-01-01T00:00:00Z', transitions_count: 0,
    });
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce({ items: [row(1), row(2)], page: 1, page_size: 2, total: 4, has_next: true, sort: 'x' } as Page<RunSummary>)
      .mockResolvedValueOnce({ items: [row(3), row(4)], page: 2, page_size: 2, total: 4, has_next: false, sort: 'x' } as Page<RunSummary>);
    const all = (await walkAllPages(fetcher as never, {})) as RunSummary[];
    expect(all.map((r) => r.id)).toEqual(['r1', 'r2', 'r3', 'r4']);
  });

  it('terminates at the maxPages safety cap if a buggy server returns has_next forever', async () => {
    const fetcher = vi.fn().mockResolvedValue({
      items: [],
      page: 1,
      page_size: 200,
      total: 999,
      has_next: true,
      sort: 'x',
    } as Page<RunSummary>);
    const all = await walkAllPages(fetcher as never, {}, 3);
    expect(fetcher).toHaveBeenCalledTimes(3);
    expect(all).toEqual([]);
  });
});

describe('isPageEnvelope', () => {
  it('returns true for a well-formed envelope', () => {
    expect(isPageEnvelope({ items: [], has_next: false, page: 1, page_size: 50, total: 0, sort: 'x' })).toBe(true);
  });
  it('returns false for null / arrays / primitives', () => {
    expect(isPageEnvelope(null)).toBe(false);
    expect(isPageEnvelope([])).toBe(false);
    expect(isPageEnvelope('string')).toBe(false);
    expect(isPageEnvelope(42)).toBe(false);
  });
  it('returns false for objects missing items', () => {
    expect(isPageEnvelope({ has_next: false })).toBe(false);
  });
  it('returns false for objects where has_next is not boolean', () => {
    expect(isPageEnvelope({ items: [], has_next: 'true' })).toBe(false);
  });
});
