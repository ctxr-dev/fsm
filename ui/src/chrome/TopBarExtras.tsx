/**
 * Topbar extras — the W18c additions to the existing TopBar:
 *
 *   - Universal search button → opens the command palette
 *   - Theme cycle button
 *   - Density cycle button
 *   - Notification bell (with unread badge)
 *   - Keyboard help button
 *
 * Wires into the existing TopBar in app.tsx as a single insertion
 * point — the rest of the chrome (logo, nav, SSE pill) stays.
 */

import { useCallback, useMemo } from 'preact/hooks';
import type { JSX } from 'preact';

import {
  commandPaletteOpen,
  commandPaletteSeed,
  densityMode,
  keyboardHelpOpen,
  notifications,
  notificationCentreOpen,
  theme,
  type DensityMode,
  type ThemeMode,
} from '../lib/store';

const DENSITY_CYCLE: DensityMode[] = ['compact', 'comfortable', 'spacious'];
const THEME_CYCLE: ThemeMode[] = ['auto', 'light', 'dark'];

function cycle<T>(values: readonly T[], current: T): T {
  const idx = values.indexOf(current);
  return values[(idx + 1) % values.length];
}

export function TopBarExtras(): JSX.Element {
  const unread = useMemo(() => notifications.value.filter((n) => !n.read).length, [notifications.value]);

  const openPalette = useCallback(() => {
    commandPaletteSeed.value = '';
    commandPaletteOpen.value = true;
  }, []);

  const cycleTheme = useCallback(() => {
    theme.value = cycle(THEME_CYCLE, theme.value);
  }, []);

  const cycleDensity = useCallback(() => {
    densityMode.value = cycle(DENSITY_CYCLE, densityMode.value);
  }, []);

  const openHelp = useCallback(() => {
    keyboardHelpOpen.value = true;
  }, []);

  const openNotifications = useCallback(() => {
    notificationCentreOpen.value = !notificationCentreOpen.value;
  }, []);

  return (
    <div class="topbar-extras flex items-center gap-1">
      <button
        type="button"
        onClick={openPalette}
        aria-label="Open command palette (⌘K)"
        title="Command palette (⌘K)"
        class="inline-flex h-8 items-center gap-1 px-2 rounded-md text-xs text-slate-500 hover:text-slate-900 dark:text-slate-400 dark:hover:text-slate-100 hover:bg-slate-100 dark:hover:bg-slate-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500"
      >
        <svg viewBox="0 0 20 20" fill="currentColor" class="w-3.5 h-3.5" aria-hidden="true">
          <path
            fill-rule="evenodd"
            d="M9 3.5a5.5 5.5 0 100 11 5.5 5.5 0 000-11zM2 9a7 7 0 1112.452 4.391l3.328 3.329a.75.75 0 11-1.06 1.06l-3.329-3.328A7 7 0 012 9z"
            clip-rule="evenodd"
          />
        </svg>
        <span class="hidden sm:inline">Search</span>
        <kbd class="hidden md:inline-block text-[10px] font-mono bg-slate-100 dark:bg-slate-700 rounded px-1 ml-1">
          ⌘K
        </kbd>
      </button>

      <button
        type="button"
        onClick={cycleDensity}
        aria-label={`Density (currently ${densityMode.value})`}
        title={`Density: ${densityMode.value}`}
        class="inline-flex h-8 w-8 items-center justify-center rounded-md text-slate-500 hover:bg-slate-100 dark:text-slate-400 dark:hover:bg-slate-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500"
      >
        <svg viewBox="0 0 20 20" fill="currentColor" class="w-3.5 h-3.5" aria-hidden="true">
          <path d="M3 4a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1zM3 9a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1zM3 14a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1z" />
        </svg>
      </button>

      <button
        type="button"
        onClick={cycleTheme}
        aria-label={`Theme (currently ${theme.value})`}
        title={`Theme: ${theme.value}`}
        class="inline-flex h-8 w-8 items-center justify-center rounded-md text-slate-500 hover:bg-slate-100 dark:text-slate-400 dark:hover:bg-slate-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500"
      >
        {theme.value === 'dark' ? (
          <svg viewBox="0 0 20 20" fill="currentColor" class="w-4 h-4" aria-hidden="true">
            <path d="M17.293 13.293A8 8 0 016.707 2.707a8.001 8.001 0 1010.586 10.586z" />
          </svg>
        ) : theme.value === 'light' ? (
          <svg viewBox="0 0 20 20" fill="currentColor" class="w-4 h-4" aria-hidden="true">
            <path
              fill-rule="evenodd"
              d="M10 2a1 1 0 011 1v1a1 1 0 11-2 0V3a1 1 0 011-1zm4 8a4 4 0 11-8 0 4 4 0 018 0zm-.464 4.95l.707.707a1 1 0 001.414-1.414l-.707-.707a1 1 0 00-1.414 1.414zm2.12-10.607a1 1 0 010 1.414l-.706.707a1 1 0 11-1.414-1.414l.707-.707a1 1 0 011.414 0zM17 11a1 1 0 100-2h-1a1 1 0 100 2h1zm-7 4a1 1 0 011 1v1a1 1 0 11-2 0v-1a1 1 0 011-1zM5.05 6.464A1 1 0 106.465 5.05l-.708-.707a1 1 0 00-1.414 1.414l.707.707zm1.414 8.486l-.707.707a1 1 0 01-1.414-1.414l.707-.707a1 1 0 011.414 1.414zM4 11a1 1 0 100-2H3a1 1 0 000 2h1z"
              clip-rule="evenodd"
            />
          </svg>
        ) : (
          <svg viewBox="0 0 20 20" fill="currentColor" class="w-4 h-4" aria-hidden="true">
            <path
              fill-rule="evenodd"
              d="M10 18a8 8 0 100-16 8 8 0 000 16zM10 4a6 6 0 016 6h-6V4z"
              clip-rule="evenodd"
            />
          </svg>
        )}
      </button>

      <button
        type="button"
        onClick={openNotifications}
        aria-label={`Notifications (${unread} unread)`}
        title="Notifications"
        class="relative inline-flex h-8 w-8 items-center justify-center rounded-md text-slate-500 hover:bg-slate-100 dark:text-slate-400 dark:hover:bg-slate-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500"
      >
        <svg viewBox="0 0 20 20" fill="currentColor" class="w-4 h-4" aria-hidden="true">
          <path d="M10 2a6 6 0 00-6 6v3.586l-.707.707A1 1 0 004 14h12a1 1 0 00.707-1.707L16 11.586V8a6 6 0 00-6-6zM10 18a3 3 0 01-3-3h6a3 3 0 01-3 3z" />
        </svg>
        {unread > 0 ? (
          <span
            aria-hidden="true"
            class="absolute -top-0.5 -right-0.5 inline-flex items-center justify-center min-w-[14px] h-[14px] px-1 rounded-full bg-red-500 text-white text-[9px] font-medium"
          >
            {unread > 9 ? '9+' : unread}
          </span>
        ) : null}
      </button>

      <button
        type="button"
        onClick={openHelp}
        aria-label="Keyboard shortcuts (?)"
        title="Keyboard shortcuts"
        class="inline-flex h-8 w-8 items-center justify-center rounded-md text-slate-500 hover:bg-slate-100 dark:text-slate-400 dark:hover:bg-slate-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500"
      >
        <span class="font-mono text-sm">?</span>
      </button>
    </div>
  );
}

export default TopBarExtras;
