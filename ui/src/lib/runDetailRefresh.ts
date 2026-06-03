/**
 * Per-pane refresh nonces for the run-detail page.
 *
 * The single SSE subscription owned by ``runDetail.tsx`` dispatches
 * incoming events to debounced refetchers. The route also bumps the
 * per-pane nonces below so lazily-mounted panels (eg. AdminSheetBody's
 * Drift / Signatures / Tool calls sections) can react to the same
 * event burst without having to open their OWN ``EventStream`` (which
 * would double-subscribe the connection limit and double the server's
 * SSE fan-out work).
 *
 * Each section reads the nonce from a ``useSignal``-style accessor in
 * its render body so dependency tracking is automatic. When the nonce
 * changes, the section's ``useEffect`` re-runs and refetches the data.
 *
 * Why one nonce PER kind (and not one global "bump everything"):
 *
 *   - State-tree refetches are noisy on chatty runs; we do not want
 *     them to invalidate the signatures section's render every time a
 *     ``state_entered`` lands.
 *   - The debouncers in ``runDetail.tsx`` already coalesce bursts per
 *     kind, so the nonces likewise tick at most once per coalesced
 *     burst — they are NOT bumped per raw SSE frame.
 *
 * Tests reset the signals by calling ``resetRunDetailRefresh()`` in a
 * ``beforeEach`` so leaked state from a prior test cannot influence
 * the next one.
 */

import { signal, type Signal } from '@preact/signals';

export const stateTreeRefreshNonce: Signal<number> = signal(0);
export const driftRefreshNonce: Signal<number> = signal(0);
export const signaturesRefreshNonce: Signal<number> = signal(0);
export const toolCallsRefreshNonce: Signal<number> = signal(0);

/** Bump the state-tree refresh nonce. */
export function bumpStateTreeRefresh(): void {
  stateTreeRefreshNonce.value = stateTreeRefreshNonce.value + 1;
}

/** Bump the drift refresh nonce. */
export function bumpDriftRefresh(): void {
  driftRefreshNonce.value = driftRefreshNonce.value + 1;
}

/** Bump the commit-signatures refresh nonce. */
export function bumpSignaturesRefresh(): void {
  signaturesRefreshNonce.value = signaturesRefreshNonce.value + 1;
}

/** Bump the tool-calls refresh nonce. */
export function bumpToolCallsRefresh(): void {
  toolCallsRefreshNonce.value = toolCallsRefreshNonce.value + 1;
}

/** Reset every nonce to zero. Useful in test ``beforeEach`` hooks. */
export function resetRunDetailRefresh(): void {
  stateTreeRefreshNonce.value = 0;
  driftRefreshNonce.value = 0;
  signaturesRefreshNonce.value = 0;
  toolCallsRefreshNonce.value = 0;
}
