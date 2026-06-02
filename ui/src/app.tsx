/**
 * App shell — router, navigation chrome, connection-state indicator,
 * toast container, and the singleton SSE pump that keeps signals fresh.
 *
 * Why this file owns the SSE stream
 * ---------------------------------
 *
 * The :class:`EventStream` is a long-lived resource: it holds an HTTP/1.1
 * socket open, schedules reconnect timers, and feeds the shared signal
 * store. We want exactly one instance per browser session — anything more
 * doubles the server's fan-out load and produces phantom event duplicates
 * in the UI tape. The app shell is the natural owner because it mounts
 * once at boot and is the last component to unmount.
 *
 * Why ``connectionState`` is mirrored, not shared
 * -----------------------------------------------
 *
 * :class:`EventStream` keeps its own :class:`Signal` so it can be unit-
 * tested in isolation without depending on the global store. The shell
 * wires that internal signal into the global :data:`connectionState`
 * signal via ``effect``, so leaf components (and the topbar pill below)
 * never need a reference to the stream. If we ever swap the SSE
 * implementation for WebSocket, the only file that has to change is this
 * one.
 *
 * Routing model
 * -------------
 *
 * The five top-level routes form the IA of the dashboard:
 *
 * * ``/runs``         — list of runs grouped by status (default landing).
 * * ``/runs/:id``     — per-run detail (state tree, journal, lock, tape).
 * * ``/specs``        — FSM spec registry (versions, definitions).
 * * ``/consumers``    — bus topology (producers + consumers).
 * * ``/settings``     — API token, theme, dev affordances.
 *
 * Routes are declared inline as thin stub components so this wave can
 * land without depending on the per-view implementations. Each stub is a
 * single-screen placeholder that calls out the route's eventual purpose;
 * subsequent waves replace the stubs with real views without touching
 * the shell.
 */

import './theme.css';

import type { JSX } from 'preact';
import { useEffect } from 'preact/hooks';
import { effect } from '@preact/signals';
import { LocationProvider, Router, Route, useLocation } from 'preact-iso';

import { ToastContainer } from './components';
import { EventStream } from './lib/sse';
import {
  appendEvent,
  setConnectionState,
} from './lib/store';
import { ROUTES } from './routes';
import { SheetHost } from './chrome/SheetHost';
import { CommandPalette } from './chrome/CommandPalette';
import { KeyboardHelp } from './chrome/KeyboardHelp';
import { NotificationCentre } from './chrome/NotificationCentre';
import { ReconnectingOverlay } from './chrome/ReconnectingOverlay';
import { ThemeApplier } from './chrome/ThemeApplier';
import { InfoTopBar } from './chrome/InfoTopBar';

// ---------------------------------------------------------------------------
// SSE wiring
// ---------------------------------------------------------------------------

/**
 * Resolve the SSE stream URL.
 *
 * Honors ``VITE_API_BASE`` (same env knob :data:`api` uses in
 * ``lib/api.ts``) so dev / prod overrides apply uniformly to both the
 * REST client and the event stream. Defaults to ``/api/v1`` so the Vite
 * dev proxy picks up the request without further configuration.
 */
function resolveStreamUrl(): string {
  const base =
    (typeof import.meta !== 'undefined' &&
      (import.meta as ImportMeta & {
        env?: Record<string, string | undefined>;
      }).env?.VITE_API_BASE) ||
    '/api/v1';
  // Trim trailing slash so we never produce ``//events/stream``.
  return `${base.replace(/\/+$/, '')}/events/stream`;
}

/**
 * Hook: open the background :class:`EventStream` on mount, close it on
 * unmount, and mirror its state into the global signals.
 *
 * The dashboard names itself ``ui-dashboard`` as the SSE ``consumer_name``
 * — the server uses that label to disambiguate live tail subscribers
 * from durable consumers and to record the ``last_seen_at`` heartbeat
 * we render on ``/consumers``. We deliberately do not pass a ``kinds``
 * filter; the shell wants the full firehose so any future view can
 * react to any event kind without re-opening the stream.
 */
function useEventStreamPump(): void {
  useEffect(() => {
    const stream = new EventStream(resolveStreamUrl(), {
      consumer_name: 'ui-dashboard',
    });

    // Mirror the stream's internal connection signal into the global
    // store so chrome components don't need a reference to the stream.
    // ``effect`` returns its own dispose, so we compose both disposals
    // into the React-style cleanup below.
    const disposeMirror = effect(() => {
      setConnectionState(stream.connectionState.value);
    });

    // Tape every real frame into the shared event log. The signal store
    // caps the tape automatically; we just hand off the frame here.
    const unsubscribe = stream.on((event) => {
      appendEvent(event);
    });

    return () => {
      unsubscribe();
      disposeMirror();
      stream.close();
    };
    // Empty deps — the stream is process-lifetime; we never want it to
    // re-open on re-render.
  }, []);
}

// ---------------------------------------------------------------------------
// Chrome — sidebar, topbar, connection pill
// ---------------------------------------------------------------------------

interface NavLinkDef {
  href: string;
  label: string;
  /** Match prefix — ``/runs`` should also light up under ``/runs/:id``. */
  matchPrefix: string;
}

// W19 sidebar splits into two groups: PRIMARY (spec-first workflow:
// just Specs) and ADMIN (cross-spec views). The split makes the
// spec-first IA visually obvious — users see Specs at the top, and
// the cross-spec admin tools sit below a divider as secondary.
import { primaryRoutes, adminRoutes } from './routes';

function toNavLink(r: { path: string; label: string }): NavLinkDef {
  return { href: r.path, label: r.label, matchPrefix: r.path };
}

const PRIMARY_NAV: readonly NavLinkDef[] = primaryRoutes().map(toNavLink);
const ADMIN_NAV: readonly NavLinkDef[] = adminRoutes().map(toNavLink);

/** Highlight a nav link when the current path lives under its prefix. */
function isActive(currentPath: string, prefix: string): boolean {
  if (prefix === '/') return currentPath === '/';
  return currentPath === prefix || currentPath.startsWith(`${prefix}/`);
}

/**
 * Sidebar — branded logo, primary nav, vertical layout.
 *
 * Renders as a semantic ``<nav>`` with ``aria-label="Primary"`` so screen
 * readers can jump to it via the landmark rotor. Each link is a real
 * ``<a>`` so :class:`LocationProvider`'s click interceptor catches them
 * (it only intercepts left-clicks on anchors with matching origin).
 */
function NavLink({ link, active }: { link: NavLinkDef; active: boolean }): JSX.Element {
  return (
    <li>
      <a
        href={link.href}
        aria-current={active ? 'page' : undefined}
        class={[
          'block rounded-md px-3 py-2 text-sm font-medium',
          'transition-colors duration-(--duration-fast) ease-(--ease-default)',
          'focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500',
          active
            ? 'bg-emerald-50 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-100'
            : 'text-slate-700 hover:bg-slate-100 dark:text-slate-200 dark:hover:bg-slate-700',
        ].join(' ')}
      >
        {link.label}
      </a>
    </li>
  );
}

function Sidebar(): JSX.Element {
  const { path } = useLocation();
  return (
    <nav
      aria-label="Primary"
      class="flex h-full w-56 shrink-0 flex-col gap-4 border-r border-slate-200 bg-white px-4 py-6 dark:border-slate-700 dark:bg-slate-800"
    >
      <a
        href="/specs"
        class="flex items-center gap-2 text-lg font-semibold text-slate-900 dark:text-slate-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500 rounded-sm"
      >
        <span
          aria-hidden="true"
          class="inline-block h-6 w-6 rounded-md bg-emerald-500"
        />
        <span>ctxr-fsm</span>
      </a>
      <ul class="flex flex-col gap-1">
        {PRIMARY_NAV.map((link) => (
          <NavLink key={link.href} link={link} active={isActive(path, link.matchPrefix)} />
        ))}
      </ul>
      {ADMIN_NAV.length > 0 ? (
        <>
          <div class="pt-3 border-t border-slate-200 dark:border-slate-700">
            <h3 class="text-[10px] uppercase tracking-wide text-slate-500 dark:text-slate-400 px-3 py-1">
              Admin
            </h3>
          </div>
          <ul class="flex flex-col gap-1">
            {ADMIN_NAV.map((link) => (
              <NavLink key={link.href} link={link} active={isActive(path, link.matchPrefix)} />
            ))}
          </ul>
        </>
      ) : null}
    </nav>
  );
}

// connectionPillProps was extracted to ``ui/src/lib/connectionPill.ts``
// in W22b3 so the new InfoTopBar can share the same mapping with the
// rest of the chrome surface. The inline TopBar component was retired
// in the same wave; InfoTopBar (chrome/InfoTopBar.tsx) renders the
// info-rich sticky header end-to-end.

// ---------------------------------------------------------------------------
// Route stubs
// ---------------------------------------------------------------------------

/**
 * Generic "view coming soon" stub.
 *
 * Each route component renders one of these so the shell is testable in
 * isolation today; later waves replace each stub with the real view
 * without touching this file.
 */
function RouteStub({
  title,
  description,
}: {
  title: string;
  description: string;
}): JSX.Element {
  return (
    <section class="p-8" aria-labelledby="route-stub-title">
      <h1
        id="route-stub-title"
        class="text-2xl font-semibold text-slate-900 dark:text-slate-100"
      >
        {title}
      </h1>
      <p class="mt-2 max-w-2xl text-base text-slate-600 dark:text-slate-300">
        {description}
      </p>
    </section>
  );
}

function NotFoundRoute(): JSX.Element {
  return (
    <RouteStub
      title="Not found"
      description="The page you requested does not exist. Use the sidebar to navigate to a known view."
    />
  );
}

// ---------------------------------------------------------------------------
// Shell
// ---------------------------------------------------------------------------

/**
 * Inner shell — mounted inside :class:`LocationProvider` so chrome
 * components can call :func:`useLocation` safely.
 *
 * The two-column layout is a fixed-width sidebar + flexible content
 * region; the content region scrolls independently of the sidebar so
 * long pages don't push the nav off-screen.
 */
function Shell(): JSX.Element {
  useEventStreamPump();
  return (
    <div class="flex h-screen w-screen overflow-hidden bg-slate-50 text-slate-900 dark:bg-slate-900 dark:text-slate-100">
      <Sidebar />
      <div class="flex min-w-0 flex-1 flex-col">
        <InfoTopBar />
        <main id="main" class="min-h-0 flex-1 overflow-auto" tabIndex={-1}>
          <Router>
            {ROUTES.map((r) => (
              <Route key={r.path} path={r.path} component={r.component} />
            ))}
            <Route default component={NotFoundRoute} />
          </Router>
        </main>
      </div>
      <ToastContainer />
      <SheetHost />
      <CommandPalette />
      <KeyboardHelp />
      <NotificationCentre />
      <ReconnectingOverlay />
      <ThemeApplier />
    </div>
  );
}

/**
 * App entry — :class:`LocationProvider` must wrap every consumer of
 * :func:`useLocation`, so the provider is the outermost component and
 * the shell renders as its sole child.
 */
export function App(): JSX.Element {
  return (
    <LocationProvider>
      <Shell />
    </LocationProvider>
  );
}

export default App;
