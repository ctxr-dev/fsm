# HTTP API

FastAPI app at `ctxr.fsm.api:app`. Mounted under `/api/v1/`. Mirrors the W4 MCP tool surface for REST/SSE clients — the UI, browser dashboards, third-party orchestrators that do not speak MCP.

Boot via the CLI:

```bash
ctxr-fsm api --host 127.0.0.1 --port 8000 --db ./.ctxr-fsm/fsm.db
```

Or as a one-liner against the default DB (`./.ctxr-fsm/fsm.db` or `$CTXR_FSM_DB`):

```bash
uvicorn ctxr.fsm.api:app --port 8000
```

Live OpenAPI / Swagger UI: **`http://<host>:<port>/docs`**. Schemas at `/openapi.json`. Both are auth-free so a browser can render them without credentials.

## Auth

Two modes, decided by the presence of `CTXR_FSM_API_TOKEN` at request time (re-read per call — no restart needed on rotation):

| Mode | `CTXR_FSM_API_TOKEN` | Behaviour |
| --- | --- | --- |
| Dev | unset / empty | Every request trusted. CORS still enforced. |
| Production | set to a non-empty string | Every request needs `Authorization: Bearer <token>`. |

In production mode:

- Missing header → `401 Unauthorized` + `WWW-Authenticate: Bearer`.
- Malformed scheme or wrong token → `403 Forbidden`.
- Comparison is constant-time (`hmac.compare_digest`).

Health probes (`/healthz`, `/readyz`) and `/docs` stay auth-free so orchestrator probes do not flap.

```bash
export CTXR_FSM_API_TOKEN=$(openssl rand -hex 32)
curl -H "Authorization: Bearer $CTXR_FSM_API_TOKEN" \
     http://127.0.0.1:8000/api/v1/projects/current
```

## CORS

The middleware is the outermost layer (pre-flight OPTIONS answered before auth fires).

Defaults: `http://localhost:5173`, `http://127.0.0.1:5173` (Vite dev server). Extend via `CTXR_FSM_API_CORS_ORIGINS` (comma-separated, exact origins only — no wildcards).

```bash
export CTXR_FSM_API_CORS_ORIGINS="https://fsm.example.com,https://ops.internal"
```

## Route catalog

All routes JSON unless noted. Every `/api/v1/*` route is auth-guarded.

### Health & metadata

| Method | Path | Auth | Body | Response |
| --- | --- | --- | --- | --- |
| `GET` | `/healthz` | no | — | `HealthResponse` |
| `GET` | `/readyz` | no | — | `ReadinessResponse` |
| `GET` | `/api/v1/projects/current` | yes | — | `ProjectMetadata` |

### Specs (registry)

See [specs.md](./specs.md) for the validation pipeline.

| Method | Path | Auth | Body | Response |
| --- | --- | --- | --- | --- |
| `GET` | `/api/v1/specs` | yes | — | `list[SpecSummary]` |
| `GET` | `/api/v1/specs/{slug}/versions?project_slug=` | yes | — | `list[SpecVersion]` |
| `GET` | `/api/v1/specs/{spec_id}` | yes | — | `SpecDetail` |
| `POST` | `/api/v1/specs` | yes | `SpecRegisterBody` | `SpecRegistered` (201) |

`POST /specs` returns `422` with `{error, message, errors|validation}` on Pydantic or cross-cutting validation failure. Byte-identical re-registration is idempotent (`created=False`).

### Runs

See [runs.md](./runs.md) and [journal.md](./journal.md).

| Method | Path | Auth | Body | Response |
| --- | --- | --- | --- | --- |
| `GET` | `/api/v1/runs?status=&since=&limit=&offset=` | yes | — | `list[RunSummary]` |
| `GET` | `/api/v1/runs/{run_id}` | yes | — | `RunDetail` |
| `GET` | `/api/v1/runs/{run_id}/state-tree` | yes | — | `StateNode` |
| `GET` | `/api/v1/runs/{run_id}/events?since_seq=&kinds=&limit=` | yes | — | `list[Event]` |
| `POST` | `/api/v1/runs/{run_id}/resume` | yes | `ResumeBody` | `ResumeResult` |
| `POST` | `/api/v1/runs/{run_id}/abort` | yes | `AbortBody` | `AbortResult` |
| `POST` | `/api/v1/runs/{run_id}/journal/{action}` | yes | — | `JournalRecovered` |

`status` accepts the literals `incomplete` / `resumable` (dispatched to `RunsRepo.incomplete` / `.resumable`) or any concrete status string. Errors:

- `404` — unknown `run_id`.
- `409` — terminal-status abort, journal replay against a still-`pending` txn, or **`fsm_spec_changed`** on resume when a newer version exists under the same slug (detail carries both hashes).
- `400` — unknown `{action}` (only `discard` / `replay` allowed).

### Events bus

See [events-bus.md](./events-bus.md) and the SSE protocol below.

| Method | Path | Auth | Body | Response |
| --- | --- | --- | --- | --- |
| `GET` | `/api/v1/events?run_id=&since_seq=&kinds=&limit=` | yes | — | `list[Event]` |
| `GET` | `/api/v1/events/stream?consumer_name=&kinds=&filter_run_id=` | yes | — | `text/event-stream` |
| `GET` | `/api/v1/producers` | yes | — | `list[Producer]` |
| `GET` | `/api/v1/consumers` | yes | — | `list[Consumer]` |
| `POST` | `/api/v1/consumers/{consumer_id}/ack` | yes | `AckBody` | `AckResult` |

`limit` is hard-capped server-side (1000 for polling reads, 2000 for `/runs/{id}/events`).

### Admin / observability

See [enforcement.md](./enforcement.md) for the W12 substrate behind `tool_calls` / `drift_signals` / `commit_signatures`.

| Method | Path | Auth | Body | Response |
| --- | --- | --- | --- | --- |
| `GET` | `/api/v1/admin/journal_txns?status=&limit=` | yes | — | `list[JournalTxn]` |
| `GET` | `/api/v1/admin/locks` | yes | — | `list[Lock]` |
| `GET` | `/api/v1/admin/tool_calls?run_id=&limit=` | yes | — | `list[ToolCall]` |
| `GET` | `/api/v1/admin/drift_signals?run_id=` | yes | — | `DriftSignalsResponse` |
| `GET` | `/api/v1/admin/commit_signatures?run_id=` | yes | — | `list[CommitSignatureRecord]` |
| `POST` | `/api/v1/admin/db/doctor` | yes | — | `DoctorReport` |

## Request / response shapes

Full Pydantic schemas live at `/docs`. Key bodies:

```python
class SpecRegisterBody(BaseModel):
    definition: dict[str, Any]   # raw FsmSpec JSON, validated server-side
    project_slug: str = "default"

class ResumeBody(BaseModel):
    from_state: str | None = None
    journal_action: Literal["discard", "replay"] | None = None

class AbortBody(BaseModel):
    reason: str | None = None

class AckBody(BaseModel):
    event_ids: list[UUID]
```

Top-level responses:

```python
class RunDetail(BaseModel):
    manifest: dict[str, Any]          # full Run row, JSON-dumped
    state_tree: StateNode | None
    events_count: int
    journal: dict[str, Any] | None    # newest unfinalised txn or null
    lock: dict[str, Any] | None       # active lock or null
```

## SSE event stream

`GET /api/v1/events/stream` opens a long-lived `text/event-stream` connection.

### Frame format

```
event: <event-name>
data: <single-line JSON>

```

Two named frames:

| Event name | When | `data` payload |
| --- | --- | --- |
| `event` | An FSM `Event` row matches the consumer's filters | `Event.model_dump_json()` (single line) |
| `ping` | No real event has been sent for ≥ 15 s | `{}` |

Frames are separated by a blank line (the trailing `\n\n` from `sse-starlette`).

### Lifecycle

```
client                                   server
  │  GET /events/stream?consumer_name=ui  │
  │ ─────────────────────────────────────▶│  register consumer (kind=http_sse_subscriber)
  │ ◀───── 200 text/event-stream ─────────│
  │                                       │
  │ ◀── event: event\ndata: {...}\n\n ────│  drains up to 100 events/cycle, ack inline
  │ ◀── event: event\ndata: {...}\n\n ────│
  │      ... (250 ms polls between cycles)│
  │ ◀── event: ping\ndata: {}\n\n ────────│  every ≥ 15 s of silence
  │                                       │
  │ ── TCP close / disconnect ────────────▶  generator sees CancelledError → tears down
```

### Cadence & semantics

- Poll cycle: **250 ms** (`_SSE_POLL_INTERVAL_SECONDS`).
- Heartbeat: **15 s** of silence (`_SSE_HEARTBEAT_SECONDS`) — survives nginx (60 s) and Cloudflare (100 s) idle timeouts.
- Batch cap: **100 events / cycle** (`_SSE_BATCH_LIMIT`); a burst loops without sleeping until drained.
- Delivery is **at-least-once**: every row is `mark_delivered` + `ack`'d inside the SQLite txn that fetched it, *before* the frame leaves the server.

### Reconnect

The cursor is server-held — reconnect with the **same `consumer_name`** and the bus resumes from the next unacked row.

```javascript
const url = new URL("/api/v1/events/stream", location.origin);
url.searchParams.set("consumer_name", "ui-tab-7");
url.searchParams.set("filter_run_id", runId);

const es = new EventSource(url, { withCredentials: true });
es.addEventListener("event", (e) => render(JSON.parse(e.data)));
es.addEventListener("ping", () => {/* keep-alive */});
// EventSource auto-reconnects on transient drops; same consumer_name → resume.
```

`EventSource` does **not** send custom `Authorization` headers. In production mode either terminate auth at a proxy that injects the header, use a `fetch`-based SSE polyfill, or accept the token via a query parameter behind a reverse proxy. Browser-side cookies + a same-origin proxy is the recommended path.

For one-shot polling without the streaming overhead, use `GET /api/v1/events?run_id=…&since_seq=…` — same data, client-held cursor.

## Errors

Conventional FastAPI shape:

```json
{ "detail": "no run with id 'abc'" }
```

`detail` may be a string or a structured object (W12 spec-hash lock, journal-replay refusal, validation failures). HTTP codes:

| Code | When |
| --- | --- |
| `400` | Bad path argument (e.g. unknown journal action). |
| `401` | Production mode, header missing. |
| `403` | Production mode, bad / malformed token. |
| `404` | Unknown run / spec / consumer. |
| `409` | Refused state transition or spec-hash drift. |
| `422` | Spec validation failed (Pydantic or cross-cutting). |
| `500` | Internal — doctor walk failed, corrupt on-disk spec, etc. |

## Cross-references

- [runs.md](./runs.md) — run lifecycle + resume semantics.
- [events-bus.md](./events-bus.md) — producers, consumers, delivery semantics.
- [journal.md](./journal.md) — atomic-txn journal + recovery.
- [specs.md](./specs.md) — FsmSpec validation pipeline.
- [enforcement.md](./enforcement.md) — W12 tool calls, drift signals, commit signatures.
- [mcp.md](./mcp.md) — parallel MCP surface against the same SQLite substrate.
