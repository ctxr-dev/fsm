/**
 * Tests for components/KeyValueTable.tsx.
 */

import { afterEach, describe, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render } from '@testing-library/preact';

import { KeyValueTable } from '../KeyValueTable';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('KeyValueTable', () => {
  test('renders one row per entry', () => {
    const { container } = render(
      <KeyValueTable
        rows={[
          { key: 'a', value: 1 },
          { key: 'b', value: 'two' },
          { key: 'c', value: true },
        ]}
      />,
    );
    expect(container.querySelectorAll('.kv-row').length).toBe(3);
  });

  test('renders key + primitive value', () => {
    const { getByText } = render(<KeyValueTable rows={[{ key: 'host', value: 'localhost' }]} />);
    expect(getByText('host')).toBeInTheDocument();
    expect(getByText('localhost')).toBeInTheDocument();
  });

  test('renders hint below key when supplied', () => {
    const { getByText } = render(
      <KeyValueTable rows={[{ key: 'k', value: 'v', hint: 'helpful note' }]} />,
    );
    expect(getByText('helpful note')).toBeInTheDocument();
  });

  test('clicking a key fires onSelect with (key, value)', () => {
    const onSelect = vi.fn();
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal('navigator', { clipboard: { writeText } });
    const { getByText } = render(
      <KeyValueTable rows={[{ key: 'k', value: 42 }]} onSelect={onSelect} />,
    );
    fireEvent.click(getByText('k'));
    expect(onSelect).toHaveBeenCalledWith('k', 42);
  });

  test('clicking a primitive value copies its stringified form', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal('navigator', { clipboard: { writeText } });
    const { getByText } = render(<KeyValueTable rows={[{ key: 'k', value: 42 }]} />);
    fireEvent.click(getByText('42'));
    await Promise.resolve();
    expect(writeText).toHaveBeenCalledWith('42');
  });

  test('clicking a string value copies the raw string (no quotes)', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal('navigator', { clipboard: { writeText } });
    const { getByText } = render(<KeyValueTable rows={[{ key: 'k', value: 'hello' }]} />);
    fireEvent.click(getByText('hello'));
    await Promise.resolve();
    expect(writeText).toHaveBeenCalledWith('hello');
  });

  test('complex value renders an embedded JsonViewer (toolbar present)', () => {
    const { getAllByLabelText } = render(
      <KeyValueTable rows={[{ key: 'meta', value: { a: 1 } }]} />,
    );
    // JsonViewer's toolbar exposes "Copy JSON"
    expect(getAllByLabelText('Copy JSON').length).toBeGreaterThan(0);
  });

  test('renders null and undefined values verbatim', () => {
    const { getByText } = render(
      <KeyValueTable rows={[{ key: 'nil', value: null }, { key: 'u', value: undefined }]} />,
    );
    expect(getByText('null')).toBeInTheDocument();
    expect(getByText('undefined')).toBeInTheDocument();
  });

  test('caption is wired to aria-label on the dl', () => {
    const { container } = render(
      <KeyValueTable rows={[{ key: 'k', value: 'v' }]} caption="DB pragmas" />,
    );
    const dl = container.querySelector('dl');
    expect(dl?.getAttribute('aria-label')).toBe('DB pragmas');
  });
});
