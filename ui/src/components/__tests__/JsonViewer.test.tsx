/**
 * Tests for components/JsonViewer.tsx — toolbar + interaction grammar.
 *
 * @uiw/react-json-view renders its tree internally; we don't deep-test
 * the library's own DOM. We DO test the toolbar contract: copy /
 * download / mode-cycle / full-screen / search. The interaction-
 * grammar contract (chevron-only expansion, label-click selects)
 * lives in the page-level e2e battery (W18k) where a real browser can
 * exercise it; here we assert our wrappers exist and dispatch.
 *
 * Theme-switch coverage for the W20 useIsDark() hook lives in the
 * sibling __tests__/JsonViewerTheme.test.tsx (separate file because
 * vi.mock is hoisted, and the rest of this suite needs the real
 * library — mixing both in one file makes every test mock-affected).
 */

import { afterEach, describe, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render } from '@testing-library/preact';

import { JsonViewer } from '../JsonViewer';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  document.documentElement.classList.remove('dark');
});

describe('JsonViewer toolbar', () => {
  test('renders rootLabel + size badge', () => {
    const { getByText } = render(<JsonViewer value={{ a: 1 }} rootLabel="inputs" />);
    expect(getByText('inputs')).toBeInTheDocument();
    // size label is one of "X B" / "Y kB" — assert the byte unit appears
    const sizeText = document.body.textContent ?? '';
    expect(sizeText).toMatch(/\d+\s*[BkM]B?/);
  });

  test('toolbar exposes Copy / DL / Expand / fullscreen buttons', () => {
    const { getByLabelText } = render(<JsonViewer value={{ a: 1 }} />);
    expect(getByLabelText('Copy JSON')).toBeInTheDocument();
    expect(getByLabelText('Download JSON')).toBeInTheDocument();
    expect(getByLabelText(/Switch to/)).toBeInTheDocument();
    expect(getByLabelText('Open in full-screen sheet')).toBeInTheDocument();
  });

  test('Copy button writes the serialised JSON to clipboard', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal('navigator', { clipboard: { writeText } });
    const { getByLabelText } = render(<JsonViewer value={{ a: 1, b: [2, 3] }} />);
    fireEvent.click(getByLabelText('Copy JSON'));
    // Microtask flush.
    await Promise.resolve();
    expect(writeText).toHaveBeenCalledWith(JSON.stringify({ a: 1, b: [2, 3] }, null, 2));
  });

  test('Download button triggers a Blob URL anchor click', () => {
    const createObjectURL = vi.fn(() => 'blob:fake');
    const revokeObjectURL = vi.fn();
    vi.stubGlobal('URL', { ...URL, createObjectURL, revokeObjectURL });
    const click = vi.fn();
    const originalCreate = document.createElement.bind(document);
    const createSpy = vi
      .spyOn(document, 'createElement')
      .mockImplementation((tag: string) => {
        const el = originalCreate(tag);
        if (tag === 'a') (el as HTMLAnchorElement).click = click;
        return el;
      });
    const { getByLabelText } = render(<JsonViewer value={{ x: 1 }} downloadFilename="out.json" />);
    fireEvent.click(getByLabelText('Download JSON'));
    expect(createObjectURL).toHaveBeenCalled();
    expect(click).toHaveBeenCalled();
    createSpy.mockRestore();
  });

  test('Mode cycle button toggles between inline and expanded labels', () => {
    const { getByLabelText } = render(<JsonViewer value={{ a: 1 }} mode="inline" />);
    expect(getByLabelText('Switch to expanded mode')).toBeInTheDocument();
    fireEvent.click(getByLabelText('Switch to expanded mode'));
    expect(getByLabelText('Switch to inline mode')).toBeInTheDocument();
  });

  test('Search input has accessible label', () => {
    const { getByLabelText } = render(<JsonViewer value={{ a: 1 }} />);
    expect(getByLabelText('Search inside JSON')).toBeInTheDocument();
  });

  test('Cmd+F focuses the search input', () => {
    const { container, getByLabelText } = render(<JsonViewer value={{ a: 1 }} />);
    const root = container.firstChild as HTMLElement;
    const search = getByLabelText('Search inside JSON') as HTMLInputElement;
    fireEvent.keyDown(root, { key: 'f', metaKey: true });
    expect(document.activeElement).toBe(search);
  });

  test('Escape on search clears the query', () => {
    const { getByLabelText } = render(<JsonViewer value={{ a: 1 }} />);
    const search = getByLabelText('Search inside JSON') as HTMLInputElement;
    search.value = 'foo';
    fireEvent.input(search, { target: { value: 'foo' } });
    fireEvent.keyDown(search, { key: 'Escape' });
    expect(search.value).toBe('');
  });

  test('Outer section carries role=group + aria-label', () => {
    const { container } = render(<JsonViewer value={1} ariaLabel="Test viewer" />);
    const section = container.firstChild as HTMLElement;
    expect(section.getAttribute('role')).toBe('group');
    expect(section.getAttribute('aria-label')).toBe('Test viewer');
  });
});

