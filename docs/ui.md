# fsm-ui

Operator dashboard for ctxr-fsm. Vite + Preact + Tailwind v4 + Preact Signals + TypeScript.

## Architecture

```
fsm/ui/src/
  app.tsx                         Shell: TopBar, Sidebar, Router, chrome mounts
  main.tsx                        boot: loadStoredPrefs + wirePrefsPersistence + render
  theme.css                       Tailwind v4 design tokens (@theme)

  routes/                         one file per route, registered in routes/index.ts
    index.ts                      ROUTES registry: path / component / label / navGroup / shortcut
    runs.tsx                      list view
    runDetail.tsx                 single-run workbench (W18d)
    runCompare.tsx                /runs/:a/compare/:b (W18i)
    specs.tsx                     master/detail + FSM graph + version timeline (W18e)
    topology.tsx                  producers + consumers (W18f)
    drift.tsx                     drift dashboard (W18g)
    journal.tsx                   journal recovery wizard (W18h)
    consumers.tsx                 legacy /consumers (redirect-class)
    settings.tsx                  doctor report + preferences

  components/                     design-system primitives (all re-exported via index.ts)
    Button, Card, Pill, Table, EmptyState, Spinner, Diff, Timeline, Tree, Toast, Dialog
    Sheet                         right-anchored slide-out (W18b)
    JsonViewer                    syntax-coloured, chevron-only collapse, toolbar (W18b)
    CodeBlock                     monospace text with search / copy / fullscreen (W18b)
    KeyValueTable                 <dl>-based key/value with click-to-copy (W18b)
    FilterChips                   chip row with x removal + aria-live (W18b)
    Tabs                          ARIA tablist + tabpanel (W18b)
    FlowGraph                     wraps @xyflow/react for FSM graphs (W18b)

  chrome/                         singleton page-level surfaces
    SheetHost                     portal-mounted sheet stack (W18a)
    CommandPalette                Cmd+K palette with fuzzy scorer (W18c)
    KeyboardHelp                  ? overlay listing shortcuts (W18c)
    NotificationCentre            right-third panel reading notifications signal (W18c)
    ThemeApplier                  effect-only: applies theme + density to <html> (W18c)
    TopBarExtras                  5 buttons added to the TopBar (W18c)

  lib/
    api.ts                        ApiClient
    sse.ts                        EventStream
    store.ts                      global signals + persistence (W18a)
    a11y.ts                       useFocusTrap / useEscapeToClose / useBodyScrollLock (W18a)
    clipboard.ts                  navigator.clipboard + execCommand fallback (W18a)
    jsonPointer.ts                RFC 6901 helpers (W18a)
    virtualWindow.ts              useWindow for fixed-row virtualisation (W18a)
    canonicalJson.ts              key-sorted whitespace-minimal JSON + sha256Hex (W18a)
    urlState.ts                   useUrlState hook + codecs + buildSchema (W18a)
    fuzzy.ts                      fzf-style scorer for CommandPalette (W18c)
    runDetailStore.ts             RunDetailFilters signal + predicates (W18d)
    specGraph.ts                  spec.definition → FlowGraph nodes + edges (W18e)
```

## Interaction grammar (universal)

| Gesture | Semantics |
|---|---|
| Chevron click | Toggle expansion. ONLY mechanism to expand / collapse. |
| Label click | Copies semantic id (JSON Pointer / state_id / kind / producer) + emits onSelect. NEVER toggles. |
| Cmd/Ctrl + label click | Opens the node in a full-screen Sheet. |
| Double-click on label | Same as Cmd-click. |
| Cmd/Ctrl+K | Open command palette. |
| ? | Open keyboard help overlay. |
| Esc | Close top sheet / dialog / palette. |

## Theme + density

`theme` signal: `'auto' | 'light' | 'dark'` (default `auto`). `densityMode`: `'compact' | 'comfortable' | 'spacious'` (default `comfortable`). Both persisted to localStorage under `fsm-ui:prefs` and applied to `<html>` by the W18c `ThemeApplier`. Cycle via the W18c topbar buttons.

## Adding a new route

1. Write the route component under `src/routes/myroute.tsx`.
2. Add an entry to the `ROUTES` registry in `src/routes/index.ts`:
   ```ts
   { path: '/myroute', component: MyRoute, label: 'My', navGroup: 'primary', shortcut: 'g m' }
   ```
3. That's it — Shell auto-mounts the `<Route>`, Sidebar auto-includes the link, CommandPalette auto-surfaces "Navigate to My", KeyboardHelp auto-renders the `g m` chord.

## Adding a primitive

1. Write the file under `src/components/MyPrim.tsx`.
2. Re-export from `src/components/index.ts`.
3. Add `src/components/__tests__/MyPrim.test.tsx` covering every prop + edge case.
4. Run `npm test -- --run` and `npx tsc -b --noEmit`.

## Test layout

```
src/components/__tests__/   primitive unit tests (vitest + @testing-library/preact)
src/chrome/__tests__/       chrome surface tests
src/lib/__tests__/          pure lib tests + hook tests via renderHook
src/test/setup.ts           jsdom-localStorage shim + @testing-library/jest-dom
```

The Python-side e2e battery lives at `fsm/tests/integration/e2e/` and is opt-in via `--run-e2e` to avoid pytest-playwright + pytest-asyncio event-loop collision (see `fsm/tests/conftest.py`).

## CI

- `npm test -- --run` — full vitest battery.
- `npm run build` — vite build (bundle budget: < 280 kB gzipped main chunk).
- `uv run pytest` — Python unit + integration suite (e2e auto-skipped).
- `uv run pytest tests/integration/e2e/ --run-e2e` — E2E suite.
