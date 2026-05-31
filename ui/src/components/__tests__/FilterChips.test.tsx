/**
 * Tests for components/FilterChips.tsx.
 */

import { afterEach, describe, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render } from '@testing-library/preact';

import { FilterChips, type FilterChip } from '../FilterChips';

afterEach(() => cleanup());

describe('FilterChips', () => {
  test('renders nothing when chips is empty', () => {
    const { container } = render(<FilterChips chips={[]} onRemove={() => {}} />);
    expect(container.firstChild).toBeNull();
  });

  test('renders one chip per entry', () => {
    const chips: FilterChip[] = [
      { id: 'a', kind: 'state', label: 'state:emit' },
      { id: 'b', kind: 'event', label: 'kind:run_started' },
    ];
    const { getByText } = render(<FilterChips chips={chips} onRemove={() => {}} />);
    expect(getByText('state:emit')).toBeInTheDocument();
    expect(getByText('kind:run_started')).toBeInTheDocument();
  });

  test('clicking remove fires onRemove with the chip', () => {
    const onRemove = vi.fn();
    const chip: FilterChip = { id: 'a', kind: 'state', label: 'state:x' };
    const { getByLabelText } = render(<FilterChips chips={[chip]} onRemove={onRemove} />);
    fireEvent.click(getByLabelText('Remove filter state:x'));
    expect(onRemove).toHaveBeenCalledWith(chip);
  });

  test('removable=false hides the x button', () => {
    const chip: FilterChip = { id: 'a', kind: 'state', label: 'pinned', removable: false };
    const { queryByLabelText } = render(<FilterChips chips={[chip]} onRemove={() => {}} />);
    expect(queryByLabelText('Remove filter pinned')).toBeNull();
  });

  test('Clear button only renders with >1 chip + onClear provided', () => {
    const onClear = vi.fn();
    const { rerender, queryByText, getByText } = render(
      <FilterChips chips={[{ id: 'a', kind: 'k', label: 'one' }]} onRemove={() => {}} onClear={onClear} />,
    );
    expect(queryByText('Clear')).toBeNull();
    rerender(
      <FilterChips
        chips={[
          { id: 'a', kind: 'k', label: 'one' },
          { id: 'b', kind: 'k', label: 'two' },
        ]}
        onRemove={() => {}}
        onClear={onClear}
      />,
    );
    expect(getByText('Clear')).toBeInTheDocument();
    fireEvent.click(getByText('Clear'));
    expect(onClear).toHaveBeenCalledOnce();
  });

  test('Clear button absent when onClear not provided', () => {
    const { queryByText } = render(
      <FilterChips
        chips={[
          { id: 'a', kind: 'k', label: 'one' },
          { id: 'b', kind: 'k', label: 'two' },
        ]}
        onRemove={() => {}}
      />,
    );
    expect(queryByText('Clear')).toBeNull();
  });

  test('outer container has role=group + aria-live=polite', () => {
    const { container } = render(
      <FilterChips chips={[{ id: 'a', kind: 'k', label: 'x' }]} onRemove={() => {}} />,
    );
    const root = container.firstChild as HTMLElement;
    expect(root.getAttribute('role')).toBe('group');
    expect(root.getAttribute('aria-live')).toBe('polite');
  });
});
