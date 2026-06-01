/**
 * Theme-switch coverage for the W20 `useIsDark()` hook in JsonViewer.
 *
 * The main JsonViewer.test.tsx suite renders against the real
 * @uiw/react-json-view library. This file lives separately so it can
 * vi.mock the library at module scope: vi.mock is hoisted, so a single
 * file can't mix mocked + real renders cleanly without affecting every
 * test in the file. Here we capture the `style` prop our wrapper
 * passes in, toggle `<html class="dark">`, and assert the captured
 * style swaps from the light to the dark CSS-variable map.
 */

import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { act, cleanup, render } from '@testing-library/preact';

// Capture every style prop @uiw/react-json-view's default export is
// invoked with. The wrapper's `<JsonView style={jsonViewStyle} />`
// passes the active theme's CSS variable map.
const capturedStyles: Record<string, string>[] = [];
vi.mock('@uiw/react-json-view', () => ({
  default: (props: { style?: Record<string, string> }) => {
    capturedStyles.push(props.style ?? {});
    return <div data-testid="mock-json-view" />;
  },
}));

// Import AFTER the mock declaration so the resolver picks up the stub.
import { JsonViewer } from '../JsonViewer';

beforeEach(() => {
  capturedStyles.length = 0;
  document.documentElement.classList.remove('dark');
});

afterEach(() => {
  cleanup();
  document.documentElement.classList.remove('dark');
});

describe('JsonViewer theme switching (W20)', () => {
  test('renders with the light-theme CSS variables when <html> has no .dark class', () => {
    render(<JsonViewer value={{ a: 1 }} />);
    const latest = capturedStyles.at(-1) ?? {};
    // Light: key colour slate-900 (#0f172a); body slate-800 (#1e293b).
    expect(latest['--w-rjv-key-string']).toBe('#0f172a');
    expect(latest['--w-rjv-color']).toBe('#1e293b');
  });

  test('switches to dark-theme CSS variables when .dark is added to <html>', async () => {
    render(<JsonViewer value={{ a: 1 }} />);
    capturedStyles.length = 0;
    await act(async () => {
      document.documentElement.classList.add('dark');
      // MutationObserver fires async; flush a couple of microtask turns.
      await Promise.resolve();
      await Promise.resolve();
    });
    const dark = capturedStyles.at(-1) ?? {};
    // Dark: key colour slate-100 (#f1f5f9); body slate-200 (#e2e8f0);
    // string values emerald-300 (#86efac).
    expect(dark['--w-rjv-key-string']).toBe('#f1f5f9');
    expect(dark['--w-rjv-color']).toBe('#e2e8f0');
    expect(dark['--w-rjv-type-string-color']).toBe('#86efac');
  });

  test('switching back to light removes the .dark class and reapplies light vars', async () => {
    document.documentElement.classList.add('dark');
    render(<JsonViewer value={{ a: 1 }} />);
    capturedStyles.length = 0;
    await act(async () => {
      document.documentElement.classList.remove('dark');
      await Promise.resolve();
      await Promise.resolve();
    });
    const light = capturedStyles.at(-1) ?? {};
    expect(light['--w-rjv-key-string']).toBe('#0f172a');
  });
});
