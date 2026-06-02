/**
 * ``<ReconnectingOverlay>`` — full-viewport reconnecting modal for W23e.
 *
 * Mounted globally from ``app.tsx::Shell``. Returns ``null`` (no DOM)
 * unless a port-change is in flight; when active, blocks the UI with
 * a backdrop + spinner card while polling the supervisor's status
 * endpoint at 500 ms cadence. On ``success`` it redirects (UI port
 * change) or reloads (api/mcp port change); on ``failed`` it
 * dismounts + emits a toast.
 *
 * The overlay is deliberately NOT Escape-dismissible: the operator
 * might mistake "I dismissed the dialog" for "the restart succeeded"
 * and act on stale state. The only manual escape is the "Cancel and
 * reload" affordance that surfaces after 30 s of unknown / pending,
 * which clears the in-flight signal and reloads the current URL.
 */

import type { JSX } from 'preact';
import { useEffect, useState } from 'preact/hooks';

import { api, ApiError, type PortChangeStatusBody } from '../lib/api';
import { useToast } from '../components';
import { lastPortChangeRequest } from '../lib/store';

const POLL_INTERVAL_MS = 500;
const ELAPSED_TICK_MS = 1000;
const STALE_ESCALATION_S = 30;

export function ReconnectingOverlay(): JSX.Element | null {
  const req = lastPortChangeRequest.value;
  const [elapsed, setElapsed] = useState<number>(0);
  const toast = useToast();

  // Reset elapsed counter whenever a new request begins.
  useEffect(() => {
    if (!req) return undefined;
    setElapsed(0);
    const tick = window.setInterval(() => setElapsed((s) => s + 1), ELAPSED_TICK_MS);
    return () => window.clearInterval(tick);
  }, [req?.requestId]);

  // Poll for status. Acts on each terminal state (success / failed).
  useEffect(() => {
    if (!req) return undefined;
    let cancelled = false;
    const handle = window.setInterval(async () => {
      let status: PortChangeStatusBody | null = null;
      try {
        status = await api.portChangeStatus(req.requestId);
      } catch (err) {
        // ApiError on transient 5xx — keep polling; the supervisor
        // might just be mid-restart and the API itself is bouncing.
        if (err instanceof ApiError && err.status >= 500) return;
        // Other errors (network) — log and continue; the overlay
        // is the operator's situational awareness, not the data
        // truth source.
        console.warn('port-change status poll failed', err);
        return;
      }
      if (cancelled || !status) return;
      if (status.status === 'success') {
        window.clearInterval(handle);
        const newUrl = status.new_url ?? req.newUrlWhenReady;
        if (req.subsystem === 'ui') {
          // Different origin — preserve the current pathname when
          // redirecting so the operator lands back on /settings (or
          // wherever they were) on the new origin.
          const path = `${window.location.pathname}${window.location.search}`;
          window.location.replace(`${newUrl.replace(/\/$/, '')}${path}`);
        } else {
          // Same origin — full reload re-establishes SSE + re-fetches
          // /projects/current, which now reports the new port.
          window.location.reload();
        }
        return;
      }
      if (status.status === 'failed') {
        window.clearInterval(handle);
        const msg = (status.error?.message as string | undefined)
          ?? `restart of ${req.subsystem} failed`;
        toast.danger(`Port change failed: ${msg}`);
        lastPortChangeRequest.value = null;
      }
    }, POLL_INTERVAL_MS) as unknown as number;
    return () => {
      cancelled = true;
      window.clearInterval(handle);
    };
  }, [req?.requestId, toast]);

  if (!req) return null;

  const stale = elapsed >= STALE_ESCALATION_S;

  return (
    <div
      class={[
        'fixed inset-0 z-[60] flex items-center justify-center',
        'bg-slate-900/40 backdrop-blur-sm',
      ].join(' ')}
      aria-live="assertive"
      role="alertdialog"
      aria-labelledby="reconnect-title"
    >
      <div
        class={[
          'w-[min(90vw,28rem)] rounded-lg p-6 shadow-2xl',
          'border border-slate-200 dark:border-slate-700',
          'bg-white dark:bg-slate-800',
        ].join(' ')}
      >
        <h2
          id="reconnect-title"
          class="text-lg font-semibold text-slate-900 dark:text-slate-100"
        >
          Reconnecting to {req.subsystem} on port {req.newPort}…
        </h2>
        <p class="mt-2 text-sm text-slate-600 dark:text-slate-300">
          The supervisor is draining the previous {req.subsystem} process and
          spawning a fresh one on the new port. {req.subsystem === 'ui'
            ? 'This tab will redirect once the new UI server answers.'
            : 'The page will reload once the new port answers.'}
        </p>
        <div class="mt-4 flex items-center gap-3 text-xs text-slate-500 dark:text-slate-400">
          <span class="inline-block h-2 w-2 rounded-full bg-amber-500 animate-pulse" aria-hidden="true" />
          <span>{elapsed}s elapsed</span>
        </div>
        {stale ? (
          <div class="mt-4 rounded-md bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-700 p-3 text-sm">
            <p class="text-amber-900 dark:text-amber-100">
              This restart is taking longer than expected. The supervisor
              may have failed to bind the new port.
            </p>
            <button
              type="button"
              class="mt-2 inline-block rounded border border-amber-300 dark:border-amber-600 bg-white dark:bg-slate-800 px-3 py-1 text-xs font-medium text-amber-800 dark:text-amber-200 hover:bg-amber-50 dark:hover:bg-amber-900/40 focus:outline-none focus-visible:ring-2 focus-visible:ring-amber-500"
              onClick={() => {
                lastPortChangeRequest.value = null;
                window.location.reload();
              }}
            >
              Cancel and reload current page
            </button>
          </div>
        ) : null}
      </div>
    </div>
  );
}

export default ReconnectingOverlay;
