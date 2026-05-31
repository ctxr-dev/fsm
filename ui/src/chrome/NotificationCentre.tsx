/**
 * NotificationCentre — right-anchored panel listing recent lifecycle
 * events (run_aborted, run_completed, drift_threshold_breached, ...).
 *
 * Source: an effect (wired in Shell) subscribes to `eventLog` and maps
 * lifecycle event kinds into `NotificationEntry`s via
 * `pushNotification`. The centre just renders that list.
 */

import { useCallback } from 'preact/hooks';
import type { JSX } from 'preact';

import {
  markAllNotificationsRead,
  notifications,
  notificationCentreOpen,
} from '../lib/store';
import { Sheet } from '../components/Sheet';

function formatTimestamp(iso: string): string {
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleString(undefined, {
      hour12: false,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    });
  } catch {
    return iso;
  }
}

export function NotificationCentre(): JSX.Element {
  const open = notificationCentreOpen.value;
  const entries = notifications.value;

  const onClose = useCallback(() => {
    markAllNotificationsRead();
    notificationCentreOpen.value = false;
  }, []);

  return (
    <Sheet
      open={open}
      onClose={onClose}
      title={`Notifications (${entries.length})`}
      width="right-third"
      pushHistory={false}
    >
      {entries.length === 0 ? (
        <div class="text-center text-sm text-slate-500 py-12">
          No notifications yet.
        </div>
      ) : (
        <ul class="divide-y divide-slate-100 dark:divide-slate-800">
          {entries.map((n) => (
            <li
              key={n.id}
              class={[
                'py-3 px-1',
                n.read ? 'opacity-70' : '',
              ].join(' ')}
            >
              <div class="flex items-center justify-between gap-2">
                <span class="text-[10px] uppercase tracking-wide text-slate-500">{n.kind}</span>
                <span class="text-[10px] text-slate-400">{formatTimestamp(n.timestamp)}</span>
              </div>
              <div class="font-medium text-sm mt-0.5">{n.title}</div>
              {n.body ? <div class="text-xs text-slate-600 dark:text-slate-400 mt-0.5">{n.body}</div> : null}
              {n.runId ? (
                <a
                  href={`/runs/${n.runId}`}
                  class="inline-block mt-1 text-[10px] text-emerald-600 dark:text-emerald-400 hover:underline"
                >
                  Open run →
                </a>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </Sheet>
  );
}

export default NotificationCentre;
