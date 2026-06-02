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
    // The "Expand" button is only rendered when more levels exist
    // below the current expansion depth. A deeply-nested fixture makes
    // sure it shows up while we assert the rest of the toolbar.
    const { getByLabelText } = render(
      <JsonViewer value={{ a: { b: { c: { d: 1 } } } }} />,
    );
    expect(getByLabelText('Copy JSON')).toBeInTheDocument();
    expect(getByLabelText('Download JSON')).toBeInTheDocument();
    expect(getByLabelText('Expand all levels')).toBeInTheDocument();
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

  test('Expand button reveals all levels then a Collapse button replaces it', () => {
    // Deep tree so "Expand" stays visible until the user clicks it.
    const { getByLabelText, queryByLabelText } = render(
      <JsonViewer value={{ a: { b: { c: { d: 1 } } } }} mode="inline" />,
    );
    expect(getByLabelText('Expand all levels')).toBeInTheDocument();
    fireEvent.click(getByLabelText('Expand all levels'));
    // After expand-all, there is nothing more to reveal so Expand hides
    // and the Collapse mirror appears.
    expect(queryByLabelText('Expand all levels')).toBeNull();
    expect(getByLabelText('Collapse to default')).toBeInTheDocument();
    fireEvent.click(getByLabelText('Collapse to default'));
    expect(getByLabelText('Expand all levels')).toBeInTheDocument();
  });

  test('Expand button is hidden when the tree is already fully revealed', () => {
    // Primitive value (depth 0) — there is nothing more to expand
    // below the inline mode's already-revealed root, so the Expand
    // affordance must not render. (For object values with any nesting,
    // inline mode keeps Expand available because depth-0 collapse means
    // there ARE more levels to reveal.)
    const { queryByLabelText } = render(<JsonViewer value={42} />);
    expect(queryByLabelText('Expand all levels')).toBeNull();
  });

  test('search prunes the tree to only matching subtrees', async () => {
    const { container, getByLabelText } = render(
      <JsonViewer
        value={{ greet: 'hello world', list: [{ kind: 'foo' }, { kind: 'bar' }] }}
        mode="expanded"
      />,
    );
    const search = getByLabelText('Search inside JSON') as HTMLInputElement;
    search.value = 'foo';
    fireEvent.input(search, { target: { value: 'foo' } });
    await Promise.resolve();
    // The rendered tree text should now mention foo but not the
    // unrelated "hello world" string from the pruned subtree.
    const txt = container.textContent ?? '';
    expect(txt).toContain('foo');
    expect(txt).not.toContain('hello world');
  });

  test('multi-term search uses OR semantics', async () => {
    const { container, getByLabelText } = render(
      <JsonViewer
        value={{ list: [{ kind: 'foo' }, { kind: 'bar' }, { kind: 'baz' }] }}
        mode="expanded"
      />,
    );
    const search = getByLabelText('Search inside JSON') as HTMLInputElement;
    search.value = 'foo bar';
    fireEvent.input(search, { target: { value: 'foo bar' } });
    await Promise.resolve();
    const txt = container.textContent ?? '';
    expect(txt).toContain('foo');
    expect(txt).toContain('bar');
    expect(txt).not.toContain('baz');
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

