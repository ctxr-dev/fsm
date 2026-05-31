/**
 * ThemeApplier — mounted in Shell. Reads `theme` and `densityMode`
 * signals and applies the resolved values to <html>:
 *
 *   - theme="dark"   → <html class="dark">
 *   - theme="light"  → <html> (no .dark class)
 *   - theme="auto"   → mirror prefers-color-scheme (live, via matchMedia)
 *   - density=*      → <html data-density="*">
 *
 * Renders nothing. Effectful only.
 */

import { useEffect } from 'preact/hooks';
import type { JSX } from 'preact';

import { densityMode, theme } from '../lib/store';

function applyTheme(resolved: 'light' | 'dark'): void {
  if (typeof document === 'undefined') return;
  const html = document.documentElement;
  if (resolved === 'dark') html.classList.add('dark');
  else html.classList.remove('dark');
}

function applyDensity(d: string): void {
  if (typeof document === 'undefined') return;
  document.documentElement.setAttribute('data-density', d);
}

export function ThemeApplier(): JSX.Element | null {
  // Theme: explicit choice or auto via matchMedia.
  useEffect(() => {
    if (typeof window === 'undefined') return undefined;
    const mql = window.matchMedia('(prefers-color-scheme: dark)');
    const compute = () => {
      const t = theme.value;
      if (t === 'auto') return mql.matches ? 'dark' : 'light';
      return t;
    };
    applyTheme(compute());
    const onChange = () => applyTheme(compute());
    mql.addEventListener?.('change', onChange);
    // Re-apply whenever signal changes (effect re-runs on signal read).
    return () => mql.removeEventListener?.('change', onChange);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [theme.value]);

  // Density: data-attribute on html.
  useEffect(() => {
    applyDensity(densityMode.value);
  }, [densityMode.value]);

  return null;
}

export default ThemeApplier;
