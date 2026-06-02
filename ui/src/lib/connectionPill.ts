/**
 * SSE connection-pill mapping.
 *
 * Shared between the original ``app.tsx`` TopBar and the W22b3
 * ``InfoTopBar`` so the live / reconnecting / offline labelling stays
 * single-sourced. The ``'closed'`` state collapses into ``danger``
 * alongside ``'error'`` because both mean "no live updates" from the
 * user's perspective — the distinction matters only to the SSE
 * wrapper.
 */

import type { ConnectionState } from './sse';

export interface ConnectionPillProps {
  variant: 'success' | 'warning' | 'danger';
  label: string;
  title: string;
}

export function connectionPillProps(state: ConnectionState): ConnectionPillProps {
  switch (state) {
    case 'open':
      return {
        variant: 'success',
        label: 'Live',
        title: 'Connected to the event stream',
      };
    case 'connecting':
      return {
        variant: 'warning',
        label: 'Reconnecting',
        title: 'Re-establishing the event stream',
      };
    case 'error':
    case 'closed':
    default:
      return {
        variant: 'danger',
        label: 'Offline',
        title: 'Event stream disconnected — retrying',
      };
  }
}
