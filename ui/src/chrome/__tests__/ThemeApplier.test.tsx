/**
 * Tests for chrome/ThemeApplier.tsx.
 *
 * Verifies the live mirror of `theme` and `densityMode` signals to
 * <html> class+attribute. Mocks matchMedia for the 'auto' branch.
 */

import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { cleanup, render } from '@testing-library/preact';

import { ThemeApplier } from '../ThemeApplier';
import { densityMode, theme } from '../../lib/store';

let matchMediaListeners: Array<(e: MediaQueryListEvent) => void> = [];

function stubMatchMedia(prefersDark: boolean): void {
  matchMediaListeners = [];
  const fakeMql = {
    matches: prefersDark,
    media: '(prefers-color-scheme: dark)',
    addEventListener: (_kind: string, cb: (e: MediaQueryListEvent) => void) => {
      matchMediaListeners.push(cb);
    },
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    onchange: null,
    dispatchEvent: () => true,
  };
  vi.stubGlobal('matchMedia', () => fakeMql);
  window.matchMedia = (() => fakeMql) as never;
}

beforeEach(() => {
  document.documentElement.classList.remove('dark');
  document.documentElement.removeAttribute('data-density');
  theme.value = 'auto';
  densityMode.value = 'comfortable';
  stubMatchMedia(false);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('ThemeApplier', () => {
  test('applies .dark class when theme=dark', () => {
    theme.value = 'dark';
    render(<ThemeApplier />);
    expect(document.documentElement.classList.contains('dark')).toBe(true);
  });

  test('removes .dark class when theme=light', () => {
    document.documentElement.classList.add('dark');
    theme.value = 'light';
    render(<ThemeApplier />);
    expect(document.documentElement.classList.contains('dark')).toBe(false);
  });

  test('theme=auto follows prefers-color-scheme (dark match)', () => {
    stubMatchMedia(true);
    theme.value = 'auto';
    render(<ThemeApplier />);
    expect(document.documentElement.classList.contains('dark')).toBe(true);
  });

  test('theme=auto follows prefers-color-scheme (light match)', () => {
    stubMatchMedia(false);
    theme.value = 'auto';
    render(<ThemeApplier />);
    expect(document.documentElement.classList.contains('dark')).toBe(false);
  });

  test('density applied as data-density attribute', () => {
    densityMode.value = 'compact';
    render(<ThemeApplier />);
    expect(document.documentElement.getAttribute('data-density')).toBe('compact');
  });

  test('renders nothing in the DOM (effect-only)', () => {
    const { container } = render(<ThemeApplier />);
    expect(container.firstChild).toBeNull();
  });
});
