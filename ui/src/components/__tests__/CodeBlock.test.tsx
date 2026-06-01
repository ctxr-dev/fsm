/**
 * Tests for components/CodeBlock.tsx.
 */

import { afterEach, describe, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render } from '@testing-library/preact';

import { CodeBlock } from '../CodeBlock';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('CodeBlock', () => {
  test('renders each line', () => {
    const text = 'line one\nline two\nline three';
    const { container } = render(<CodeBlock text={text} lineNumbers={true} />);
    const lines = container.querySelectorAll('.cb-line');
    expect(lines.length).toBe(3);
  });

  test('omits gutter when lineNumbers=false', () => {
    const { container } = render(<CodeBlock text="x" lineNumbers={false} />);
    expect(container.querySelector('.cb-gutter')).toBeNull();
  });

  test('shows gutter automatically when text exceeds 5 lines', () => {
    const text = Array.from({ length: 10 }, (_, i) => `line ${i}`).join('\n');
    const { container } = render(<CodeBlock text={text} />);
    expect(container.querySelector('.cb-gutter')).not.toBeNull();
  });

  test('Copy button writes full text to clipboard', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal('navigator', { clipboard: { writeText } });
    const { getByLabelText } = render(<CodeBlock text="payload-xyz" />);
    fireEvent.click(getByLabelText('Copy code'));
    await Promise.resolve();
    expect(writeText).toHaveBeenCalledWith('payload-xyz');
  });

  test('search highlights matching substring with <mark>', () => {
    const { container, getByLabelText } = render(
      <CodeBlock text="hello world\nfoo bar world\nbaz" />,
    );
    const search = getByLabelText('Search inside code') as HTMLInputElement;
    fireEvent.input(search, { target: { value: 'world' } });
    const marks = container.querySelectorAll('mark.jv-match');
    expect(marks.length).toBeGreaterThanOrEqual(2);
  });

  test('search is case-insensitive', () => {
    const { container, getByLabelText } = render(<CodeBlock text="Hello\nhELLO" />);
    const search = getByLabelText('Search inside code') as HTMLInputElement;
    fireEvent.input(search, { target: { value: 'hello' } });
    const marks = container.querySelectorAll('mark.jv-match');
    expect(marks.length).toBe(2);
  });

  test('Cmd+F focuses the search input', () => {
    const { container, getByLabelText } = render(<CodeBlock text="x" />);
    const root = container.firstChild as HTMLElement;
    const search = getByLabelText('Search inside code') as HTMLInputElement;
    fireEvent.keyDown(root, { key: 'f', ctrlKey: true });
    expect(document.activeElement).toBe(search);
  });

  test('Escape clears the search', () => {
    const { getByLabelText } = render(<CodeBlock text="x" />);
    const search = getByLabelText('Search inside code') as HTMLInputElement;
    fireEvent.input(search, { target: { value: 'foo' } });
    fireEvent.keyDown(search, { key: 'Escape' });
    expect(search.value).toBe('');
  });

  test('outer section has role=group with aria-label', () => {
    const { container } = render(<CodeBlock text="x" ariaLabel="prompt" />);
    const section = container.firstChild as HTMLElement;
    expect(section.getAttribute('role')).toBe('group');
    expect(section.getAttribute('aria-label')).toBe('prompt');
  });

  test('language label appears in toolbar', () => {
    const { getByText } = render(<CodeBlock text="x" language="python" />);
    expect(getByText('python')).toBeInTheDocument();
  });

  test('zero-length text renders one empty line', () => {
    const { container } = render(<CodeBlock text="" />);
    expect(container.querySelectorAll('.cb-line').length).toBe(1);
  });

  // -------------------------------------------------------------------------
  // W21: markdown render path (toggle + auto-detect + view sync + sanitise)
  // -------------------------------------------------------------------------

  test('markdown content auto-defaults to Rendered view + exposes a Raw toggle', () => {
    const md = '# Hello\n\n- one\n- two\n\n```js\nconst x = 1;\n```\n';
    const { getByText, container } = render(<CodeBlock text={md} />);
    expect(getByText('Raw')).toBeInTheDocument();
    expect(container.querySelector('h1')?.textContent).toBe('Hello');
  });

  test('plain text shows no markdown toggle and stays in Raw view', () => {
    const { queryByText } = render(<CodeBlock text="just plain text no markdown" />);
    expect(queryByText('Raw')).toBeNull();
    expect(queryByText('Rendered')).toBeNull();
  });

  test('clicking Raw toggle flips to raw monospace view', () => {
    const md = '# Title\n\nbody';
    const { getByText, container } = render(<CodeBlock text={md} />);
    fireEvent.click(getByText('Raw'));
    expect(container.querySelector('h1')).toBeNull();
    expect(getByText('Rendered')).toBeInTheDocument();
  });

  test('view auto-syncs back to Raw when content stops being markdown-eligible', () => {
    const md = '# Title';
    const plain = 'plain';
    const { rerender, queryByText } = render(<CodeBlock text={md} />);
    expect(queryByText('Raw')).not.toBeNull();
    rerender(<CodeBlock text={plain} />);
    expect(queryByText('Raw')).toBeNull();
    expect(queryByText('Rendered')).toBeNull();
  });

  test('rendered markdown preserves GFM task-list checkboxes (disabled inputs)', () => {
    // Comment in CodeBlock claims GFM features render; verify the
    // task-list path actually does — the sanitiser must NOT strip
    // the disabled checkbox markup marked.js generates for `- [ ]`
    // and `- [x]` items.
    const md = '- [ ] open task\n- [x] done task\n';
    const { container } = render(<CodeBlock text={md} />);
    const inputs = container.querySelectorAll('.cb-markdown input[type=checkbox]');
    expect(inputs.length).toBe(2);
    expect((inputs[0] as HTMLInputElement).disabled).toBe(true);
    expect((inputs[1] as HTMLInputElement).disabled).toBe(true);
    expect((inputs[1] as HTMLInputElement).checked).toBe(true);
  });

  test('rendered markdown forbids img / svg / iframe / script for security', () => {
    const dangerous = [
      '# Safe',
      '',
      '![img](https://example.com/x.png)',
      '<iframe src="https://evil.example.com"></iframe>',
      '<svg onload="alert(1)"><circle r="10"/></svg>',
      '<script>alert(1)</script>',
    ].join('\n');
    const { container } = render(<CodeBlock text={dangerous} />);
    const body = container.querySelector('.cb-markdown');
    expect(body).not.toBeNull();
    const html = body!.innerHTML;
    expect(html).not.toContain('<img');
    expect(html).not.toContain('<iframe');
    expect(html).not.toContain('<svg');
    expect(html).not.toContain('<script');
    expect(html).not.toContain('onload=');
    expect(html).toContain('Safe');
  });
});
