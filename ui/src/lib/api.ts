/**
 * Typed HTTP client for the W5 FSM REST surface.
 *
 * Mirrors every Pydantic shape produced by ``ctxr.fsm.api.routes_*``
 * field-for-field so consumers of this client get the same payloads a
 * Python client would. The shapes here are hand-written (no codegen
 * for the W6 MVP); a follow-up wave will swap to ``openapi-typescript``
 * derived types once the spec is frozen.
 *
 * All API calls flow through :class:`ApiClient`, which centralises JSON
 * encoding, query-string formatting, bearer-token auth, and error
 * mapping. Failures surface as :class:`ApiError` carrying the raw
 * status code plus the parsed ``detail`` body (or the raw text when
 * the body is not JSON) — UI code matches on ``err.status`` to render
 * tailored affordances (404 → "not found"; 409 → "wrong state"; etc.)
 * without re-parsing the response.
 */

// ---------------------------------------------------------------------------
// Wire-level value-object types (Pydantic parallels)
// ---------------------------------------------------------------------------

/** Decoded JSON-object payload — matches Python ``dict[str, Any]``. */
export type JsonObject = Record<string, unknown>;

/** Trimmed-down run row used by ``GET /runs``. */
export interface RunSummary {
  id: string;
  project_id: string;
  fsm_spec_id: string;
  status: string;
  current_state: string | null;
  next_state: string | null;
  verdict: string | null;
  started_at: string;
  ended_at: string | null;
  last_update_at: string;
  transitions_count: number;
}

/** Full run manifest as stored in the ``runs`` table. */
export interface RunManifest {
  id: string;
  project_id: string;
  fsm_spec_id: string;
  fsm_spec_hash: string;
  status: string;
  current_state: string | null;
  next_state: string | null;
  verdict: string | null;
  started_at: string;
  ended_at: string | null;
  last_update_at: string;
  paused_at: string | null;
  pause_reason: string | null;
  parent_run_id: string | null;
  resume_history: unknown[];
  args: JsonObject;
  metadata: JsonObject;
  transitions_count: number;
}

/** Lock row as serialised by the HTTP layer (``_lock_to_dict``). */
export interface LockSnapshot {
  run_id: string;
  holder_session_id: string;
  acquired_at: string;
  expires_at: string;
  is_stale: boolean;
}

/** Journal-txn row as serialised by the HTTP layer (``_journal_to_dict``). */
export interface JournalState {
  id: string;
  run_id: string;
  status: 'pending' | 'ready_to_finalise' | 'finalised' | string;
  staged_writes: JsonObject[];
  started_at: string;
  ready_at: string | null;
  finalised_at: string | null;
}

/** Full per-run report returned by ``GET /runs/{run_id}``. */
export interface RunDetail {
  manifest: RunManifest;
  state_tree: StateNode | null;
  events_count: number;
  journal: JournalState | null;
  lock: LockSnapshot | null;
}

/** Self-referential node in the state-entry tree. */
export interface StateNode {
  entry_id: string;
  state_id: string;
  entry_seq: number;
  entered_at: string;
  exited_at: string | null;
  status: string;
  inputs: JsonObject;
  outputs: JsonObject;
  iteration_n: number | null;
  children: StateNode[];
}

/** One row of the event journal. */
export interface Event {
  id: string;
  run_id: string | null;
  kind: string;
  producer_id: string;
  payload: JsonObject;
  created_at: string;
  seq: number | null;
}

/** Trimmed spec row used by ``GET /api/v1/specs``. */
export interface SpecSummary {
  id: string;
  project_id: string;
  project_slug: string;
  slug: string;
  version: number;
  hash: string;
  created_at: string;
}

/** Per-version row from ``GET /api/v1/specs/{slug}/versions``. */
export interface SpecVersion {
  id: string;
  project_id: string;
  project_slug: string;
  slug: string;
  version: number;
  hash: string;
  created_at: string;
}

/** Full spec record (with canonical ``definition`` body). */
export interface SpecDetail {
  id: string;
  project_id: string;
  project_slug: string;
  slug: string;
  version: number;
  hash: string;
  definition: JsonObject;
  registered_at: string;
}

/** Response shape of ``POST /api/v1/specs``. */
export interface SpecRegistered {
  spec_id: string;
  hash: string;
  version: number;
  slug: string;
  project_id: string;
  project_slug: string;
  created: boolean;
}

/** A registered event producer. */
export interface Producer {
  id: string;
  kind: string;
  name: string;
  metadata: JsonObject;
  created_at: string;
}

/** A registered event consumer. */
export interface Consumer {
  id: string;
  kind: string;
  name: string;
  filter_kind: string[] | null;
  filter_run_id: string | null;
  created_at: string;
  last_seen_at: string | null;
}

/** Response shape of ``POST /runs/{run_id}/resume``. */
export interface ResumeResult {
  run_id: string;
  from_state: string | null;
  journal_action: string | null;
  journal_txn_id: string | null;
  engine_resume: string;
}

/** Response shape of ``POST /runs/{run_id}/abort``. */
export interface AbortResult {
  run_id: string;
  previous_status: string;
  new_status: string;
  ended_at: string;
  reason: string | null;
}

/** Response shape of ``POST /runs/{run_id}/journal/{action}``. */
export interface JournalRecovered {
  run_id: string;
  action: 'discard' | 'replay' | string;
  acted: boolean;
  txn_id: string | null;
  note: string | null;
}

/** Response shape of ``POST /consumers/{id}/ack``. */
export interface AckResult {
  consumer_id: string;
  requested: number;
  acked: number;
}

/** Admin: per-row journal txn snapshot. */
export interface JournalTxn {
  id: string;
  run_id: string;
  status: 'pending' | 'ready_to_finalise' | 'finalised' | string;
  staged_writes: JsonObject[];
  started_at: string;
  ready_at: string | null;
  finalised_at: string | null;
}

/** Admin: locks-table row. */
export interface Lock {
  run_id: string;
  holder_session_id: string;
  acquired_at: string;
  expires_at: string;
}

/** Admin: tool-call audit row. */
export interface ToolCall {
  id: string;
  run_id: string | null;
  producer_id: string;
  tool_name: string;
  args_redacted: JsonObject;
  succeeded: boolean;
  created_at: string;
}

/** Admin: drift signal row. */
export interface DriftSignal {
  id: string;
  run_id: string;
  producer_id: string;
  signal_kind: string;
  weight: number;
  payload: JsonObject;
  created_at: string;
}

/** Admin: drift signals + aggregate score for a run. */
export interface DriftSignalsResponse {
  run_id: string;
  score: number;
  signals: DriftSignal[];
}

/** Admin: commit signature envelope row. */
export interface CommitSignatureRecord {
  id: string;
  run_id: string;
  state_id: string;
  iteration_n: number | null;
  brief_id: string;
  inputs_hash: string;
  outputs_hash: string;
  session_id: string;
  signature: string;
  verified: boolean;
  created_at: string;
}

/** Admin: doctor-report payload (mirrors ``ctxr-fsm doctor``). */
export interface DoctorReport {
  /** Absolute filesystem path of the open DB file when filesystem-
   *  backed. For non-file backends (`:memory:`, `file:`-URI variants)
   *  this is the raw `engine.url.database` segment (e.g. `:memory:`
   *  or `file:test.db`). When the URL has no `database` component
   *  (`sqlite://`), falls back to the rendered `str(engine.url)`.
   *  Distinguish a real path from a sentinel by checking whether
   *  `project_root` / `db_path_relative` are non-null. */
  db_path: string;
  /** Absolute path of the project root that hosts ``.ctxr-fsm/``.
   *  ``null`` when the DB has no filesystem path (in-memory / non-
   *  file backends — derivation is meaningless then). ``undefined``
   *  when talking to a server older than W22. */
  project_root?: string | null;
  /** DB path rendered relative to ``project_root``. UI surfaces
   *  prefer this so the displayed value stays portable. Canonical
   *  layout: ``.ctxr-fsm/fsm.db``. ``null`` when ``project_root`` is
   *  also ``null``; ``undefined`` on a pre-W22 server. */
  db_path_relative?: string | null;
  pragmas: JsonObject;
  tables_with_row_counts: Record<string, number>;
  alembic_revision: string | null;
  journal_txn_breakdown: Record<string, number>;
  lock_count: number;
}

/**
 * One row of the ``ProjectMetadata.subsystems`` map.
 *
 * Mirrors :class:`SubsystemInfo` in ``ctxr/fsm/api/__init__.py``. The
 * supervisor's discovery doc (``.ctxr-fsm/active-mcp.json``) carries
 * one of these per running subsystem (``mcp`` / ``api`` / ``ui``);
 * the info-rich topbar uses them to render a clickable status pill
 * for each subsystem with the right URL + a re-probe target.
 */
export interface SubsystemInfo {
  base_url: string;
  healthz_url: string | null;
  pid: number | null;
}

/**
 * Response shape of ``GET /api/v1/projects/current``.
 *
 * Mirrors :class:`ProjectMetadata` in ``ctxr/fsm/api/__init__.py``.
 * ``project_slug`` / ``project_root`` / ``db_path_relative`` /
 * ``subsystems`` are all population-state-dependent (empty when the
 * supervisor hasn't written ``active-mcp.json`` yet, or when the DB
 * is in-memory).
 */
export interface ProjectMetadata {
  fsm_version: string;
  project_open: boolean;
  project_slug: string | null;
  db_path: string;
  project_root: string | null;
  db_path_relative: string | null;
  swagger_url: string;
  api_base_url: string;
  subsystems: Record<string, SubsystemInfo>;
}

// ---------------------------------------------------------------------------
// Request parameter shapes
// ---------------------------------------------------------------------------

/**
 * Generic page envelope returned by every list endpoint.
 *
 * Mirrors :class:`Page` on the Python side (ctxr/fsm/api/_pagination.py)
 * verbatim — ``items`` is the slice, ``total`` is the population size,
 * ``has_next`` is derived (kept on the wire so a client that lost the
 * ``page`` / ``page_size`` can still chain). The post-W22b2 wire shape
 * for every list endpoint is ``Page<T>``; the only legacy raw-array
 * endpoints left are the ones that don't paginate (``getStateTree``,
 * ``getRun``, etc., which return a single document).
 */
export interface Page<T> {
  items: T[];
  page: number;
  page_size: number;
  total: number;
  has_next: boolean;
  sort: string;
}

/** Common pagination params used by every paginated list endpoint. */
export interface PageParams {
  page?: number;
  page_size?: number;
  sort?: string;
}

/** Query parameters for ``GET /runs``. */
export interface ListRunsParams extends PageParams {
  status?: string;
  since?: string;
}

/** Query parameters for ``GET /runs/{id}/events``. */
export interface GetEventsParams extends PageParams {
  since_seq?: number;
  kinds?: string[];
}

/** Query parameters for ``GET /events``. */
export interface ListEventsParams extends PageParams {
  run_id: string;
  since_seq?: number;
  kinds?: string[];
}

/** Body of ``POST /runs/{run_id}/resume``. */
export interface ResumeBody {
  from_state?: string;
  journal_action?: 'discard' | 'replay';
}

/** Body of ``POST /runs/{run_id}/abort``. */
export interface AbortBody {
  reason?: string;
}

/** Body of ``POST /api/v1/specs``. */
export interface RegisterSpecBody {
  definition: JsonObject;
  project_slug?: string;
}

/** Query parameters for ``GET /admin/journal_txns``. */
export interface ListJournalTxnsParams extends PageParams {
  status?: 'pending' | 'ready_to_finalise' | 'finalised';
}

/** Query parameters for ``GET /admin/tool_calls``. */
export interface ListToolCallsParams extends PageParams {
  run_id: string;
}

/** Query parameters for ``GET /specs/{slug}/versions``. */
export interface ListSpecVersionsParams extends PageParams {
  project_slug?: string;
}

// ---------------------------------------------------------------------------
// Error type
// ---------------------------------------------------------------------------

/**
 * Raised by :class:`ApiClient` for every non-2xx response.
 *
 * ``status`` is the HTTP status code; ``detail`` is the parsed ``detail``
 * field of the JSON body (the FastAPI / HTTPException convention) when
 * the response is JSON, otherwise the raw response text. ``body``
 * carries the full decoded payload so callers can inspect structured
 * error envelopes (e.g. the spec validator's ``{error, validation}``
 * shape) without re-parsing.
 */
export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;
  readonly body: unknown;

  constructor(status: number, detail: unknown, body: unknown, message?: string) {
    super(message ?? `API request failed: HTTP ${status}`);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
    this.body = body;
  }
}

/**
 * Raised when a 2xx response from the API carries a payload shape the
 * caller cannot use — wrong content-type (HTML instead of JSON), a JSON
 * body that fails to parse, or a Page envelope missing required fields.
 *
 * Distinct from :class:`ApiError` so route-level UX can render an
 * actionable "UI / server out of sync" affordance (Reload page) rather
 * than the generic "Failed to load X" with the server's error message.
 * The triggers are typically operator-facing rather than server-side
 * bugs: a stale UI bundle, a dev-server proxy mis-route, a CDN edge
 * cache returning HTML where JSON was expected.
 *
 * ``received`` carries the actual payload (the HTML body or the
 * malformed envelope) so diagnostics can show the operator exactly
 * what the server returned. ``hint`` is the human-readable guidance
 * for recovery.
 */
export class ApiResponseShapeError extends Error {
  readonly received: unknown;
  readonly hint: string;

  constructor(message: string, received: unknown, hint: string) {
    super(`${message} — ${hint}`);
    this.name = 'ApiResponseShapeError';
    this.received = received;
    this.hint = hint;
  }
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

/** Acceptable query-string value types. */
type QueryValue =
  | string
  | number
  | boolean
  | null
  | undefined
  | readonly (string | number | boolean)[];

/**
 * Build a ``?a=1&b=2`` query string from a record.
 *
 * Skips ``null`` / ``undefined`` entries (so callers can spread optional
 * params without pre-filtering); arrays are repeated (``kinds=a&kinds=b``)
 * to match FastAPI's ``list[str]`` query convention used by
 * ``GET /events`` and ``GET /runs/{id}/events``.
 */
function buildQuery(params?: Record<string, QueryValue>): string {
  if (!params) return '';
  const usp = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null) continue;
    if (Array.isArray(value)) {
      for (const item of value) {
        if (item === undefined || item === null) continue;
        usp.append(key, String(item));
      }
    } else {
      usp.append(key, String(value));
    }
  }
  const qs = usp.toString();
  return qs ? `?${qs}` : '';
}

/**
 * Normalise an HTTP-style method name for ``fetch`` (no surprises if a
 * caller passes ``'get'`` instead of ``'GET'``).
 */
type HttpMethod = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';

// ---------------------------------------------------------------------------
// ApiClient
// ---------------------------------------------------------------------------

/**
 * Typed HTTP client for the W5 API surface.
 *
 * ``baseUrl`` defaults to ``'/api/v1'`` so the Vite dev server's proxy
 * picks up the call without further config; pass an absolute URL when
 * the UI is deployed against a remote API. ``token`` populates the
 * ``Authorization: Bearer …`` header on every request — leave it
 * unset for dev mode where ``CTXR_FSM_API_TOKEN`` is also unset.
 *
 * Every method delegates to :meth:`request`, which is the single
 * choke-point for JSON encoding, query-string formatting, bearer
 * auth, and error mapping. New endpoints should follow the same
 * shape so we never grow more than one fetch wrapper.
 */
export class ApiClient {
  /** Base URL with no trailing slash. */
  readonly baseUrl: string;

  /** Bearer token; ``undefined`` skips the ``Authorization`` header. */
  token?: string;

  /** Optional fetch implementation (injected by tests). */
  private readonly fetchImpl: typeof fetch;

  constructor(
    baseUrl: string = '/api/v1',
    token?: string,
    fetchImpl?: typeof fetch,
  ) {
    // Trim a trailing slash so every ``request`` call's path can lead
    // with ``/`` and we never produce ``//foo``.
    this.baseUrl = baseUrl.replace(/\/+$/, '');
    this.token = token;
    // Bind to ``globalThis`` so we never lose the implicit ``this``
    // that ``fetch`` needs in the browser (it throws otherwise).
    this.fetchImpl = fetchImpl ?? fetch.bind(globalThis);
  }

  // -------------------------------------------------------------------------
  // Generic request
  // -------------------------------------------------------------------------

  /**
   * Issue an HTTP request and return the parsed JSON body (or ``undefined``
   * for ``204 No Content`` responses).
   *
   * Throws :class:`ApiError` on any non-2xx response. ``path`` must
   * start with ``/``; it is appended directly to ``baseUrl``. Query
   * parameters are formatted via :func:`buildQuery`; the body (when
   * present) is JSON-encoded with ``Content-Type: application/json``.
   */
  async request<T>(
    method: HttpMethod,
    path: string,
    params?: Record<string, QueryValue>,
    body?: unknown,
  ): Promise<T> {
    const url = `${this.baseUrl}${path}${buildQuery(params)}`;
    const headers: Record<string, string> = {
      Accept: 'application/json',
    };
    if (this.token) {
      headers.Authorization = `Bearer ${this.token}`;
    }
    let serialisedBody: string | undefined;
    if (body !== undefined) {
      headers['Content-Type'] = 'application/json';
      serialisedBody = JSON.stringify(body);
    }

    const response = await this.fetchImpl(url, {
      method,
      headers,
      body: serialisedBody,
    });

    // ``204`` carries no body — return undefined typed as ``T`` so
    // callers whose response type allows void do not have to special-
    // case it. (We never advertise ``T = void`` for an endpoint that
    // returns a payload, so the cast is safe in practice.)
    if (response.status === 204) {
      return undefined as T;
    }

    const rawText = await response.text();
    let parsed: unknown = undefined;
    let parseFailed = false;
    if (rawText.length > 0) {
      try {
        parsed = JSON.parse(rawText);
      } catch {
        // Non-JSON body — keep ``parsed`` as undefined and surface the
        // raw text via the error path below if the response failed.
        parsed = rawText;
        parseFailed = true;
      }
    }

    if (!response.ok) {
      const detail =
        parsed && typeof parsed === 'object' && parsed !== null && 'detail' in parsed
          ? (parsed as { detail: unknown }).detail
          : parsed;
      const message =
        typeof detail === 'string'
          ? `API ${method} ${path} failed (HTTP ${response.status}): ${detail}`
          : `API ${method} ${path} failed (HTTP ${response.status})`;
      throw new ApiError(response.status, detail, parsed, message);
    }

    // W23a Layer 2: a 2xx response with a non-JSON body is a wire
    // violation — the most common trigger is the Vite dev-server SPA
    // fallback serving ``index.html`` because the proxied API target
    // isn't reachable on the configured port. Pre-W23a the code below
    // silently returned the HTML string as if it were the response
    // payload, which then crashed every downstream consumer that read
    // ``.items`` / ``.id`` / etc. on a string. Surface it as a typed
    // error instead so route-level code can render an actionable
    // "UI / server out of sync" affordance.
    const contentType = (response.headers.get('content-type') ?? '').toLowerCase();
    const looksJson = contentType.includes('application/json') || contentType.includes('+json');
    if (rawText.length > 0 && !looksJson) {
      const sniff = rawText.slice(0, 80).replace(/\s+/g, ' ');
      throw new ApiResponseShapeError(
        `Expected JSON from ${method} ${path}, got ${contentType || 'unknown content-type'}`,
        rawText,
        'Endpoint may be unregistered or the dev-server proxy may have fallen ' +
          `back to the SPA shell. First 80 bytes: "${sniff}".`,
      );
    }
    if (rawText.length > 0 && parseFailed) {
      throw new ApiResponseShapeError(
        `Body of ${method} ${path} was not valid JSON`,
        rawText,
        'Server returned a non-JSON 2xx body — likely an HTML error page or a misconfigured proxy.',
      );
    }

    return parsed as T;
  }

  // -------------------------------------------------------------------------
  // Runs
  // -------------------------------------------------------------------------

  /** ``GET /runs`` — list runs with optional filters. */
  listRuns(params: ListRunsParams = {}): Promise<Page<RunSummary>> {
    return this.request<Page<RunSummary>>('GET', '/runs', {
      status: params.status,
      since: params.since,
      page: params.page,
      page_size: params.page_size,
      sort: params.sort,
    });
  }

  /** ``GET /runs/{id}`` — full per-run report. */
  getRun(id: string): Promise<RunDetail> {
    return this.request<RunDetail>('GET', `/runs/${encodeURIComponent(id)}`);
  }

  /** ``GET /runs/{id}/state-tree`` — nested state-entry tree. */
  getStateTree(id: string): Promise<StateNode> {
    return this.request<StateNode>(
      'GET',
      `/runs/${encodeURIComponent(id)}/state-tree`,
    );
  }

  /** ``GET /runs/{id}/events`` — slice of the run's event journal. */
  getEvents(id: string, params: GetEventsParams = {}): Promise<Page<Event>> {
    return this.request<Page<Event>>(
      'GET',
      `/runs/${encodeURIComponent(id)}/events`,
      {
        since_seq: params.since_seq,
        kinds: params.kinds,
        page: params.page,
        page_size: params.page_size,
        sort: params.sort,
      },
    );
  }

  /** ``POST /runs/{id}/resume`` — resume a paused / faulted run. */
  resumeRun(id: string, body: ResumeBody = {}): Promise<ResumeResult> {
    return this.request<ResumeResult>(
      'POST',
      `/runs/${encodeURIComponent(id)}/resume`,
      undefined,
      body,
    );
  }

  /** ``POST /runs/{id}/abort`` — mark an in-flight run as aborted. */
  abortRun(id: string, body: AbortBody = {}): Promise<AbortResult> {
    return this.request<AbortResult>(
      'POST',
      `/runs/${encodeURIComponent(id)}/abort`,
      undefined,
      body,
    );
  }

  /** ``POST /runs/{id}/journal/{action}`` — recover the pending journal txn. */
  recoverJournal(
    id: string,
    action: 'discard' | 'replay',
  ): Promise<JournalRecovered> {
    return this.request<JournalRecovered>(
      'POST',
      `/runs/${encodeURIComponent(id)}/journal/${encodeURIComponent(action)}`,
    );
  }

  // -------------------------------------------------------------------------
  // Specs
  // -------------------------------------------------------------------------

  /** ``GET /specs`` — every registered spec across every project. */
  listSpecs(params: PageParams = {}): Promise<Page<SpecSummary>> {
    return this.request<Page<SpecSummary>>('GET', '/specs', {
      page: params.page,
      page_size: params.page_size,
      sort: params.sort,
    });
  }

  /** ``GET /specs/{slug}/versions`` — version history for one FSM slug. */
  getSpecVersions(
    slug: string,
    params: ListSpecVersionsParams = {},
  ): Promise<Page<SpecVersion>> {
    return this.request<Page<SpecVersion>>(
      'GET',
      `/specs/${encodeURIComponent(slug)}/versions`,
      {
        project_slug: params.project_slug,
        page: params.page,
        page_size: params.page_size,
        sort: params.sort,
      },
    );
  }

  /** ``GET /specs/{spec_id}`` — full spec record (with ``definition``). */
  getSpec(specId: string): Promise<SpecDetail> {
    return this.request<SpecDetail>(
      'GET',
      `/specs/${encodeURIComponent(specId)}`,
    );
  }

  /** ``POST /specs`` — register a freshly-supplied FSM spec. */
  registerSpec(body: RegisterSpecBody): Promise<SpecRegistered> {
    return this.request<SpecRegistered>(
      'POST',
      '/specs',
      undefined,
      {
        definition: body.definition,
        project_slug: body.project_slug ?? 'default',
      },
    );
  }

  // -------------------------------------------------------------------------
  // Producers / Consumers
  // -------------------------------------------------------------------------

  /** ``GET /producers`` — bus topology, producer side. */
  listProducers(params: PageParams = {}): Promise<Page<Producer>> {
    return this.request<Page<Producer>>('GET', '/producers', {
      page: params.page,
      page_size: params.page_size,
      sort: params.sort,
    });
  }

  /** ``GET /consumers`` — bus topology, consumer side. */
  listConsumers(params: PageParams = {}): Promise<Page<Consumer>> {
    return this.request<Page<Consumer>>('GET', '/consumers', {
      page: params.page,
      page_size: params.page_size,
      sort: params.sort,
    });
  }

  /** ``POST /consumers/{id}/ack`` — acknowledge a batch of events. */
  ackEvents(consumerId: string, eventIds: string[]): Promise<AckResult> {
    return this.request<AckResult>(
      'POST',
      `/consumers/${encodeURIComponent(consumerId)}/ack`,
      undefined,
      { event_ids: eventIds },
    );
  }

  // -------------------------------------------------------------------------
  // Admin
  // -------------------------------------------------------------------------

  /** ``GET /admin/journal_txns`` — journal-txn ledger across runs. */
  listJournalTxns(
    params: ListJournalTxnsParams = {},
  ): Promise<Page<JournalTxn>> {
    return this.request<Page<JournalTxn>>('GET', '/admin/journal_txns', {
      status: params.status,
      page: params.page,
      page_size: params.page_size,
      sort: params.sort,
    });
  }

  /** ``GET /admin/locks`` — every currently-held lock row. */
  listLocks(params: PageParams = {}): Promise<Page<Lock>> {
    return this.request<Page<Lock>>('GET', '/admin/locks', {
      page: params.page,
      page_size: params.page_size,
      sort: params.sort,
    });
  }

  /** ``GET /admin/tool_calls`` — tool-call audit log for a run. */
  listToolCalls(params: ListToolCallsParams): Promise<Page<ToolCall>> {
    return this.request<Page<ToolCall>>('GET', '/admin/tool_calls', {
      run_id: params.run_id,
      page: params.page,
      page_size: params.page_size,
      sort: params.sort,
    });
  }

  /** ``GET /admin/drift_signals`` — drift signals + aggregate score. */
  listDriftSignals(runId: string): Promise<DriftSignalsResponse> {
    return this.request<DriftSignalsResponse>(
      'GET',
      '/admin/drift_signals',
      { run_id: runId },
    );
  }

  /** ``GET /admin/commit_signatures`` — commit-signature timeline. */
  listCommitSignatures(
    runId: string,
    params: PageParams = {},
  ): Promise<Page<CommitSignatureRecord>> {
    return this.request<Page<CommitSignatureRecord>>(
      'GET',
      '/admin/commit_signatures',
      {
        run_id: runId,
        page: params.page,
        page_size: params.page_size,
        sort: params.sort,
      },
    );
  }

  /** ``POST /admin/db/doctor`` — diagnostic dump of the project DB. */
  doctor(): Promise<DoctorReport> {
    return this.request<DoctorReport>('POST', '/admin/db/doctor');
  }

  /**
   * ``GET /projects/current`` — full project + subsystem discovery doc.
   *
   * Returns everything the info-rich topbar needs to render in one
   * round-trip: fsm version, project slug, project root, db_path
   * (absolute + relative), Swagger / API base URLs derived from the
   * request, and the supervisor's subsystem map (mcp / api / ui base
   * URLs + healthz URLs + pids) read from
   * ``<project_root>/.ctxr-fsm/active-mcp.json``.
   */
  getCurrentProject(): Promise<ProjectMetadata> {
    return this.request<ProjectMetadata>('GET', '/projects/current');
  }
}

/**
 * Maximum page size the server accepts in a single request. Mirrors
 * ``MAX_PAGE_SIZE`` in ``ctxr/fsm/api/_pagination.py`` (200). Exposed so
 * routes that legitimately need to enumerate the full population
 * (e.g. spec catalog grouping, drift dashboard seeding) can request
 * the largest page in one round trip.
 */
export const MAX_PAGE_SIZE = 200;

/**
 * Walk every page of a paginated endpoint and return the flattened
 * row list.
 *
 * The post-W22b2 wire format paginates every list endpoint, but a few
 * UI surfaces still need the FULL population (derivative aggregations
 * like "count runs per spec slug", sibling tree expansion, drift
 * dashboard seeding). Rather than each route re-implementing the same
 * "loop with ``has_next`` + safety stop" pattern, this helper owns it.
 *
 * Pages are fetched serially so the server's paginator sees a stable
 * cursor / sort across requests. ``page_size`` is fixed at
 * :const:`MAX_PAGE_SIZE` so we minimise the round-trip count.
 * ``maxPages`` (default 50 = 10,000 rows at max page size) is a
 * safety stop that catches runaway loops if a future server bug
 * returns ``has_next: true`` indefinitely; callers that legitimately
 * need more can raise it explicitly.
 *
 * Example:
 *
 * ```ts
 * const allSpecs = await walkAllPages(
 *   (p) => api.listSpecs(p),
 *   {},
 * );
 * ```
 */
export async function walkAllPages<T, P extends PageParams>(
  fetcher: (params: P) => Promise<Page<T>>,
  baseParams: Omit<P, 'page' | 'page_size'>,
  maxPages = 50,
): Promise<T[]> {
  const out: T[] = [];
  for (let page = 1; page <= maxPages; page += 1) {
    const env = await fetcher({
      ...(baseParams as P),
      page,
      page_size: MAX_PAGE_SIZE,
    } as P);
    // W23a Layer 1: defensive envelope-shape check. If something
    // upstream returned a non-Page<T> body the existing ``ApiClient``
    // checks should have caught it (W23a Layer 2 — content-type +
    // JSON-parse guards on every 2xx), but defence in depth catches
    // the residual cases: a fetcher swap in tests that returned a
    // raw array, a future endpoint that opted out of Page<T>, a
    // genuinely missing ``items``/``has_next`` field after a partial
    // wire-format rollback. Surfaces as ApiResponseShapeError so the
    // calling route can render a "reload page" affordance rather
    // than crashing with ``TypeError: env.items is not iterable``.
    if (!isPageEnvelope<T>(env)) {
      throw new ApiResponseShapeError(
        'walkAllPages: fetcher returned a non-Page<T> envelope',
        env,
        'The UI bundle expects {items, page, has_next, ...} from this ' +
          'endpoint. Common causes: (1) the API process is unreachable on ' +
          'the dev-server proxy port (Vite served the SPA shell instead), ' +
          '(2) the UI bundle is stale relative to the server. Refresh the ' +
          'page; if it persists, restart the supervisor.',
      );
    }
    out.push(...env.items);
    if (!env.has_next) return out;
  }
  return out;
}

/**
 * Type guard for a ``Page<T>`` envelope shape. Defensive predicate the
 * helper functions use before spreading ``items`` so a wire-format
 * regression surfaces as a typed error rather than a JS TypeError.
 */
export function isPageEnvelope<T>(v: unknown): v is Page<T> {
  return (
    typeof v === 'object' &&
    v !== null &&
    Array.isArray((v as { items?: unknown }).items) &&
    typeof (v as { has_next?: unknown }).has_next === 'boolean'
  );
}

/**
 * Process-wide :class:`ApiClient` instance with the dev-server defaults.
 *
 * Components that just need to call the API import this directly:
 *
 * ```ts
 * import { api } from '@/lib/api';
 * const runs = await api.listRuns({ status: 'incomplete' });
 * ```
 *
 * Tests construct their own :class:`ApiClient` with a stub
 * ``fetchImpl`` so the global instance never leaks across test cases.
 */
export const api: ApiClient = new ApiClient(
  // Read the override at import time; Vite inlines ``import.meta.env``
  // at build time so the resulting bundle is a constant string.
  (typeof import.meta !== 'undefined' &&
    (import.meta as ImportMeta & { env?: Record<string, string | undefined> }).env
      ?.VITE_API_BASE) ||
    '/api/v1',
);
