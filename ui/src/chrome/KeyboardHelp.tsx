/**
 * Keyboard help overlay (`?` toggles).
 *
 * Reads `keyboardHelpOpen` signal. Lists every global shortcut + the
 * `g <key>` chord table derived from the ROUTES registry.
 */

import { useEffect } from 'preact/hooks';
import type { JSX } from 'preact';

import { keyboardHelpOpen } from '../lib/store';
import { shortcutRoutes } from '../routes';
import { Dialog } from '../components/Dialog';

interface ShortcutRow {
  keys: string;
  action: string;
}

const GLOBAL_SHORTCUTS: ShortcutRow[] = [
  { keys: 'Cmd/Ctrl+K', action: 'Open command palette' },
  { keys: '?', action: 'Open this help' },
  { keys: '/', action: 'Focus search (opens palette)' },
  { keys: 'Esc', action: 'Close top sheet / dialog / palette' },
];

const CHORD_DESCRIPTION = 'g <key>';

export function KeyboardHelp(): JSX.Element | null {
  const open = keyboardHelpOpen.value;

  // Global ?-to-open shortcut. The body of the page handles it,
  // unless focus is inside an input.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== '?') return;
      const target = e.target as HTMLElement | null;
      if (
        target &&
        (target.tagName === 'INPUT' ||
          target.tagName === 'TEXTAREA' ||
          target.isContentEditable)
      ) {
        return;
      }
      e.preventDefault();
      keyboardHelpOpen.value = !keyboardHelpOpen.value;
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  if (!open) return null;

  return (
    <Dialog
      open={open}
      onClose={() => (keyboardHelpOpen.value = false)}
      title="Keyboard shortcuts"
      widthClassName="max-w-xl"
    >
      <div class="space-y-5 text-sm">
        <section>
          <h3 class="text-xs uppercase tracking-wide text-slate-500 mb-2">Global</h3>
          <dl class="grid grid-cols-[10rem_1fr] gap-y-1">
            {GLOBAL_SHORTCUTS.map((s) => (
              <>
                <dt class="font-mono text-xs text-slate-600 dark:text-slate-400">{s.keys}</dt>
                <dd>{s.action}</dd>
              </>
            ))}
          </dl>
        </section>
        <section>
          <h3 class="text-xs uppercase tracking-wide text-slate-500 mb-2">
            Navigate ({CHORD_DESCRIPTION} chord)
          </h3>
          <dl class="grid grid-cols-[10rem_1fr] gap-y-1">
            {shortcutRoutes().map((r) => (
              <>
                <dt class="font-mono text-xs text-slate-600 dark:text-slate-400">{r.shortcut}</dt>
                <dd>{r.label}</dd>
              </>
            ))}
          </dl>
        </section>
      </div>
    </Dialog>
  );
}

export default KeyboardHelp;
