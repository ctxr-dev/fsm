/**
 * Single source of truth for every route the dashboard knows about.
 *
 * Consumed by:
 *   - `app.tsx` Shell, which maps `ROUTES` to `<Route path component>` entries.
 *   - `chrome/Sidebar.tsx` for the primary + admin nav (filtered by `navGroup`).
 *   - `chrome/CommandPalette.tsx` for "navigate to ..." actions (filtered by `shortcut`).
 *   - `chrome/KeyboardHelp.tsx` for the `g <key>` chord table.
 *
 * Adding a new route is one entry here + one component import. No
 * surgery on `app.tsx`. No drift between the nav, the palette, and the
 * keyboard help.
 *
 * `navGroup: null` means "not in nav" — applies to parameterised
 * routes like `/runs/:id` and `/runs/:id/compare/:otherId` (the user
 * doesn't navigate to them via a link; they get there by clicking a
 * row or by deep link).
 */

import type { ComponentType } from 'preact';

import { Runs } from './runs';
import { RunDetailRoute } from './runDetail';
import { RunCompareRoute } from './runCompare';
import { SpecsRoute } from './specs';
import { TopologyRoute } from './topology';
import { DriftRoute } from './drift';
import { JournalRoute } from './journal';
import { ConsumersRoute } from './consumers';
import { SettingsRoute } from './settings';

export interface RouteDef {
  /** preact-iso path pattern, e.g. `/runs/:id`. */
  path: string;
  /** The component to mount. */
  component: ComponentType<unknown>;
  /** Human-readable label (sidebar, breadcrumbs, command palette). */
  label: string;
  /** Group bucket in the sidebar. `null` = not in nav. */
  navGroup: 'primary' | 'admin' | null;
  /** Optional `g <key>` keyboard chord (e.g. `'g r'` for /runs). */
  shortcut?: string;
  /** Optional one-line hint used by the command palette / help overlay. */
  description?: string;
}

/**
 * Registry, in display order.
 *
 * Order in this array IS the sidebar order. Edit deliberately.
 */
export const ROUTES: readonly RouteDef[] = [
  {
    path: '/',
    component: Runs,
    label: 'Runs',
    navGroup: null, // alias for /runs; surfaced via /runs below
    description: 'Live and historical FSM runs.',
  },
  {
    path: '/runs',
    component: Runs,
    label: 'Runs',
    navGroup: 'primary',
    shortcut: 'g r',
    description: 'Live and historical FSM runs.',
  },
  {
    path: '/runs/:id',
    component: RunDetailRoute,
    label: 'Run detail',
    navGroup: null,
    description: 'State tree, events, journal, drift for one run.',
  },
  {
    path: '/runs/:id/compare/:otherId',
    component: RunCompareRoute,
    label: 'Run comparison',
    navGroup: null,
    description: 'Side-by-side comparison of two runs.',
  },
  {
    path: '/specs',
    component: SpecsRoute,
    label: 'Specs',
    navGroup: 'primary',
    shortcut: 'g s',
    description: 'Registered FSM specs and version lineage.',
  },
  {
    path: '/topology',
    component: TopologyRoute,
    label: 'Topology',
    navGroup: 'primary',
    shortcut: 'g t',
    description: 'Event-bus producers, consumers, and SSE health.',
  },
  // /consumers is the pre-W18f path; kept registered as a non-nav
  // entry so any bookmarks still resolve to the same list-view
  // component until users naturally migrate to /topology.
  {
    path: '/consumers',
    component: ConsumersRoute,
    label: 'Consumers (legacy)',
    navGroup: null,
    description: 'Deprecated; see /topology.',
  },
  {
    path: '/drift',
    component: DriftRoute,
    label: 'Drift',
    navGroup: 'primary',
    shortcut: 'g d',
    description: 'Runs ranked by accumulated drift score.',
  },
  {
    path: '/journal',
    component: JournalRoute,
    label: 'Journal',
    navGroup: 'admin',
    shortcut: 'g j',
    description: 'Recover pending journal transactions across runs.',
  },
  {
    path: '/settings',
    component: SettingsRoute,
    label: 'Settings',
    navGroup: 'admin',
    description: 'Project metadata, preferences, diagnostics.',
  },
];

/** Convenience filter: routes that should appear in the primary nav rail. */
export function primaryRoutes(): RouteDef[] {
  return ROUTES.filter((r) => r.navGroup === 'primary');
}

/** Convenience filter: routes that should appear under the admin section. */
export function adminRoutes(): RouteDef[] {
  return ROUTES.filter((r) => r.navGroup === 'admin');
}

/** Convenience filter: routes that carry a global keyboard shortcut. */
export function shortcutRoutes(): RouteDef[] {
  return ROUTES.filter((r) => typeof r.shortcut === 'string' && r.shortcut.length > 0);
}
