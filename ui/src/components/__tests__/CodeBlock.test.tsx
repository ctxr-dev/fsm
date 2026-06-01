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

  test('rendered markdown strips style/tabindex/autofocus/contenteditable/accesskey on ANY tag (W21 adversarial-verify)', () => {
    // Workflow finding B1+B2: these attributes work on <p>/<a>/etc.,
    // not just <input>. Test the global FORBID_ATTR scope.
    const dangerous = [
      '# Title',
      '',
      '<p style="position:fixed;inset:0;width:100vw;height:100vh">overlay</p>',
      '<p tabindex="0">focus hijack</p>',
      '<a href="https://example.com" autofocus accesskey="x" contenteditable="true">spoof</a>',
    ].join('\n');
    const { container } = render(<CodeBlock text={dangerous} />);
    const body = container.querySelector('.cb-markdown')!;
    const html = body.innerHTML;
    expect(html).not.toContain('style=');
    expect(html).not.toContain('tabindex=');
    expect(html).not.toContain('autofocus');
    expect(html).not.toContain('contenteditable');
    expect(html).not.toContain('accesskey=');
    // Legit content + href stay.
    expect(html).toContain('overlay');
    expect(html).toContain('focus hijack');
    expect(html).toContain('href="https://example.com"');
  });

  test('rendered markdown task-list checkbox accepts ONLY type/disabled/checked attrs (W21 adversarial-verify)', () => {
    // Workflow finding B1-B4: even on the surviving disabled
    // checkbox, attributes like style / tabindex / aria-label / role
    // / name / value / id leak through DOMPurify's defaults. The
    // pruneNonCheckboxInput hook now allowlists attr NAMES.
    const dangerous = [
      '- [ ] normal task',
      '<input type="checkbox" disabled style="position:fixed;inset:0">',
      '<input type="checkbox" disabled tabindex="0" aria-label="enter password" role="textbox">',
      '<input type="checkbox" disabled name="csrf" value="leaked" id="phish">',
      '<input type="checkbox" disabled="false">',
    ].join('\n');
    const { container } = render(<CodeBlock text={dangerous} />);
    const body = container.querySelector('.cb-markdown')!;
    const inputs = body.querySelectorAll('input');
    for (const input of Array.from(inputs)) {
      // EVERY attribute on every surviving input must be in the
      // allowed set.
      for (const attr of Array.from(input.attributes)) {
        expect(['type', 'disabled', 'checked']).toContain(attr.name.toLowerCase());
      }
    }
    // The disabled="false" variant should be REMOVED entirely (the
    // tightened isDisabled check rejects it).
    const html = body.innerHTML;
    expect(html).not.toContain('disabled="false"');
  });

  test('rendered markdown still preserves canonical GFM checkbox shape (regression)', () => {
    // Make sure the lockdown above hasn't accidentally broken the
    // happy path: `- [ ]` and `- [x]` MUST still render a disabled
    // checkbox with the right `checked` state.
    const md = '- [ ] open\n- [x] done\n';
    const { container } = render(<CodeBlock text={md} />);
    const inputs = container.querySelectorAll('.cb-markdown input[type=checkbox]');
    expect(inputs.length).toBe(2);
    expect((inputs[0] as HTMLInputElement).disabled).toBe(true);
    expect((inputs[0] as HTMLInputElement).checked).toBe(false);
    expect((inputs[1] as HTMLInputElement).disabled).toBe(true);
    expect((inputs[1] as HTMLInputElement).checked).toBe(true);
  });

  test('rendered markdown strips src/srcset/poster/formaction even on allowed tags', () => {
    // <input> is allowed (for GFM task-list checkboxes) so a naive
    // FORBID_TAGS list would still permit
    // `<input type="image" src="https://attacker/beacon.gif">` —
    // which fetches the URL on render and leaks the operator's IP.
    // Verify the FORBID_ATTR config strips these network-fetch
    // attributes regardless of which (allowed) tag carries them.
    const dangerous = [
      '# Title',
      '',
      '<input type="image" src="https://example.com/x.png">',
      '<a href="https://example.com" poster="https://example.com/y.gif">link</a>',
    ].join('\n');
    const { container } = render(<CodeBlock text={dangerous} />);
    const body = container.querySelector('.cb-markdown');
    expect(body).not.toBeNull();
    const html = body!.innerHTML;
    expect(html).not.toContain('src=');
    expect(html).not.toContain('poster=');
    expect(html).not.toContain('srcset=');
    expect(html).not.toContain('formaction=');
    // The legitimate href on the anchor is preserved.
    expect(html).toContain('href="https://example.com"');
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
