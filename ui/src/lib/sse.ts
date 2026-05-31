/**
 * EventSource wrapper for the W5 SSE stream (``GET /api/v1/events/stream``).
 *
 * The browser's stock :class:`EventSource` covers the easy 80% — it
 * auto-reconnects, parses ``event:`` / ``data:`` frames, and exposes a
 * cancellation handle. It is missing three things the dashboard needs:
 *
 * 1. **Exponential backoff** — :class:`EventSource` reconnects on a
 *    short, browser-defined interval (typically 3 s). The W5 server is
 *    behind a Vite proxy in dev and behind a reverse proxy in prod; a
 *    full restart can take >5 s and a tight reconnect loop drowns the
 *    server in handshake traffic. We disable the built-in reconnect
 *    (close + recreate on ``error``) and reconnect ourselves with a
 *    1 s → 2 s → 4 s → 8 s → 16 s → 30 s schedule.
 * 2. **Heartbeat filtering** — the server emits a ``ping`` frame every
 *    15 s so reverse-proxy idle timers do not kill the socket. Those
 *    frames are infrastructure noise; the UI does not want to render
 *    them. The wrapper delivers only ``event`` frames to handlers.
 * 3. **Connection-state observability** — a single ``connectionState``
 *    signal so the UI can render a "reconnecting…" pill when the
 *    socket is bouncing. Stock :class:`EventSource` exposes a
 *    ``readyState`` enum but it does not distinguish "we're between
 *    backoff attempts" from "we're actively trying"; ours does.
 *
 * Browser SSE quirks worth knowing
 * --------------------------------
 *
 * * :class:`EventSource` always sends ``GET``; bearer-token auth has
 *   to ride in the query string (the API accepts ``?token=…`` for
 *   exactly this case in dev mode). Production deployments terminate
 *   TLS in front of the server and use cookies.
 * * Cross-origin :class:`EventSource` defaults to CORS without
 *   credentials. The Vite dev proxy makes it same-origin so this is a
 *   non-issue in dev. In production, set ``withCredentials`` via the
 *   ``EventSourceInit`` second argument if cookies are needed.
 */

import { signal, type Signal } from '@preact/signals';

import type { Event as FsmEvent } from './api';

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

/** State machine for the SSE connection — surfaced via :attr:`connectionState`. */
export type ConnectionState = 'connecting' | 'open' | 'closed' | 'error';

/** Handler signature: receives one FSM :class:`Event` per real frame. */
export type EventHandler = (event: FsmEvent) => void;

/** Unsubscribe handle returned by :meth:`EventStream.on`. */
export type Unsubscribe = () => void;

// ---------------------------------------------------------------------------
// Backoff schedule
// ---------------------------------------------------------------------------

/**
 * Exponential backoff schedule in milliseconds.
 *
 * Doubles from 1 s up to a 30 s cap, then stays at 30 s for every
 * subsequent failure. The schedule is intentionally tight at the start
 * (a 1 s wobble from a Vite restart shouldn't paint a "reconnecting"
 * banner that lingers) and tops out at 30 s so a server that's been
 * down for an hour does not wait an hour to try again once it comes
 * back.
 */
const BACKOFF_SCHEDULE_MS: readonly number[] = [
  1_000, 2_000, 4_000, 8_000, 16_000, 30_000,
];

/** Pick the wait for attempt ``n`` (0-indexed); clamps at the schedule's tail. */
function backoffFor(attempt: number): number {
  const idx = Math.min(attempt, BACKOFF_SCHEDULE_MS.length - 1);
  return BACKOFF_SCHEDULE_MS[idx];
}

// ---------------------------------------------------------------------------
// EventStream
// ---------------------------------------------------------------------------

/**
 * Long-lived SSE subscription to ``GET /api/v1/events/stream``.
 *
 * Construct with the absolute or proxied URL and any query parameters
 * (``consumer_name`` is required by the server; ``kinds`` / ``filter_run_id``
 * are optional). The stream opens immediately and reconnects with
 * exponential backoff on every error until :meth:`close` is called.
 *
 * ```ts
 * const stream = new EventStream('/api/v1/events/stream', {
 *   consumer_name: 'dashboard',
 * });
 * const unsubscribe = stream.on((event) => console.log(event.kind));
 * // … later
 * unsubscribe();
 * stream.close();
 * ```
 *
 * The :attr:`connectionState` signal is updated synchronously inside
 * the event handlers — consumers that subscribe via ``effect`` will
 * re-render on every transition.
 */
export class EventStream {
  /** Full URL with the query string already attached. */
  readonly url: string;

  /** Observable connection state — wire to UI with ``effect`` / ``useSignal``. */
  readonly connectionState: Signal<ConnectionState>;

  /** Optional :class:`EventSource` factory (injected by tests). */
  private readonly EventSourceCtor: typeof EventSource;

  /** The active :class:`EventSource`, or ``null`` between attempts. */
  private source: EventSource | null = null;

  /** Registered event handlers. */
  private readonly handlers = new Set<EventHandler>();

  /** Number of consecutive failed connection attempts. */
  private attempt = 0;

  /** Pending reconnect timer, or ``null`` when no reconnect is queued. */
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  /** Set by :meth:`close` so the reconnect loop knows to give up. */
  private closed = false;

  constructor(
    url: string,
    params: Record<string, string> = {},
    EventSourceCtor?: typeof EventSource,
  ) {
    this.url = this.buildUrl(url, params);
    this.connectionState = signal<ConnectionState>('connecting');
    // ``globalThis.EventSource`` is available in browsers and jsdom
    // (with a polyfill); tests inject a stub via the third argument.
    this.EventSourceCtor =
      EventSourceCtor ??
      (globalThis as { EventSource: typeof EventSource }).EventSource;
    this.connect();
  }

  // -------------------------------------------------------------------------
  // Public surface
  // -------------------------------------------------------------------------

  /**
   * Register a handler for real event frames (heartbeats are filtered).
   *
   * Returns an unsubscribe function — call it to drop the handler
   * without closing the underlying socket. Multiple subscribers share
   * the same connection.
   */
  on(handler: EventHandler): Unsubscribe {
    this.handlers.add(handler);
    return () => {
      this.handlers.delete(handler);
    };
  }

  /**
   * Close the underlying :class:`EventSource` and stop reconnecting.
   *
   * Safe to call multiple times. After ``close()`` returns, the
   * :attr:`connectionState` signal reads ``'closed'`` and no further
   * frames will be delivered to handlers.
   */
  close(): void {
    this.closed = true;
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.source !== null) {
      this.source.close();
      this.source = null;
    }
    this.connectionState.value = 'closed';
  }

  // -------------------------------------------------------------------------
  // Internals
  // -------------------------------------------------------------------------

  /** Compose a URL with stable, sorted query parameters. */
  private buildUrl(base: string, params: Record<string, string>): string {
    const keys = Object.keys(params);
    if (keys.length === 0) return base;
    const usp = new URLSearchParams();
    for (const key of keys) {
      const value = params[key];
      if (value === undefined || value === null) continue;
      usp.append(key, value);
    }
    const qs = usp.toString();
    if (qs.length === 0) return base;
    return base.includes('?') ? `${base}&${qs}` : `${base}?${qs}`;
  }

  /** Open a fresh :class:`EventSource` and wire up the listeners. */
  private connect(): void {
    if (this.closed) return;
    this.connectionState.value = 'connecting';
    const source = new this.EventSourceCtor(this.url);
    this.source = source;

    source.onopen = () => {
      // A successful handshake resets the backoff schedule — the next
      // failure starts from 1 s again rather than 30 s.
      this.attempt = 0;
      this.connectionState.value = 'open';
    };

    // The server emits ``event: event`` frames for real FSM events.
    // ``addEventListener('event', …)`` matches that named-event frame
    // shape — ``onmessage`` would only fire for unnamed ``data:``
    // frames, which the server never sends.
    source.addEventListener('event', (rawEvent) => {
      this.handleEventFrame(rawEvent as MessageEvent<string>);
    });

    // Heartbeat frames arrive as ``event: ping``. We deliberately
    // ignore them — they exist purely to keep proxies happy.
    source.addEventListener('ping', () => {
      // No-op. Listed explicitly so the contract is visible.
    });

    source.onerror = () => {
      // ``EventSource`` will retry on its own; we close and rebuild so
      // we can apply our own backoff schedule. Browsers will surface
      // a transient network blip as an ``error`` event with
      // ``readyState === CONNECTING``; we treat every error the same
      // and let the backoff schedule absorb the noise.
      this.connectionState.value = 'error';
      if (this.source) {
        this.source.close();
        this.source = null;
      }
      this.scheduleReconnect();
    };
  }

  /** Parse one event frame and broadcast to subscribers. */
  private handleEventFrame(rawEvent: MessageEvent<string>): void {
    // ``data`` is a JSON-encoded :class:`Event` — see the server's
    // ``model_dump_json`` call in ``routes_events.py``.
    if (typeof rawEvent.data !== 'string' || rawEvent.data.length === 0) {
      return;
    }
    let parsed: FsmEvent;
    try {
      parsed = JSON.parse(rawEvent.data) as FsmEvent;
    } catch {
      // A malformed frame is logged to the console (so we notice in
      // dev) but does not tear the stream down — the next frame may
      // be fine.
      console.warn('[EventStream] dropped malformed frame:', rawEvent.data);
      return;
    }
    for (const handler of this.handlers) {
      try {
        handler(parsed);
      } catch (err) {
        // A handler crash is the handler's bug, not the stream's;
        // surface it but do not stop dispatching to other handlers.
        console.error('[EventStream] handler threw:', err);
      }
    }
  }

  /** Queue the next reconnect attempt with exponential backoff. */
  private scheduleReconnect(): void {
    if (this.closed) return;
    if (this.reconnectTimer !== null) return; // already queued
    const delay = backoffFor(this.attempt);
    this.attempt += 1;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      if (!this.closed) {
        this.connect();
      }
    }, delay);
  }
}
