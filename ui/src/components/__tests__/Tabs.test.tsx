/**
 * Tests for components/Tabs.tsx.
 */

import { afterEach, describe, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render } from '@testing-library/preact';

import { Tabs, type TabSpec } from '../Tabs';

afterEach(() => cleanup());

const TABS: TabSpec[] = [
  { id: 'a', label: 'Alpha' },
  { id: 'b', label: 'Beta' },
  { id: 'c', label: 'Gamma', disabled: true },
  { id: 'd', label: 'Delta' },
];

const PANELS = {
  a: <div>panel A</div>,
  b: <div>panel B</div>,
  c: <div>panel C</div>,
  d: <div>panel D</div>,
};

describe('Tabs', () => {
  test('renders one tab button per spec', () => {
    const { container } = render(
      <Tabs tabs={TABS} activeTab="a" onChange={() => {}} panels={PANELS} />,
    );
    expect(container.querySelectorAll('[role="tab"]').length).toBe(4);
  });

  test('renders only the active panel', () => {
    const { queryByText } = render(
      <Tabs tabs={TABS} activeTab="a" onChange={() => {}} panels={PANELS} />,
    );
    expect(queryByText('panel A')).toBeInTheDocument();
    expect(queryByText('panel B')).toBeNull();
  });

  test('aria-selected reflects activeTab', () => {
    const { container } = render(
      <Tabs tabs={TABS} activeTab="b" onChange={() => {}} panels={PANELS} />,
    );
    const tabs = container.querySelectorAll('[role="tab"]');
    expect(tabs[0].getAttribute('aria-selected')).toBe('false');
    expect(tabs[1].getAttribute('aria-selected')).toBe('true');
  });

  test('clicking a non-active tab fires onChange', () => {
    const onChange = vi.fn();
    const { getByRole } = render(
      <Tabs tabs={TABS} activeTab="a" onChange={onChange} panels={PANELS} />,
    );
    fireEvent.click(getByRole('tab', { name: /Beta/ }));
    expect(onChange).toHaveBeenCalledWith('b');
  });

  test('clicking a disabled tab is a no-op', () => {
    const onChange = vi.fn();
    const { getByRole } = render(
      <Tabs tabs={TABS} activeTab="a" onChange={onChange} panels={PANELS} />,
    );
    fireEvent.click(getByRole('tab', { name: /Gamma/ }));
    expect(onChange).not.toHaveBeenCalled();
  });

  test('ArrowRight moves to next enabled tab', () => {
    const onChange = vi.fn();
    const { container } = render(
      <Tabs tabs={TABS} activeTab="a" onChange={onChange} panels={PANELS} />,
    );
    const tablist = container.querySelector('[role="tablist"]') as HTMLElement;
    fireEvent.keyDown(tablist, { key: 'ArrowRight' });
    expect(onChange).toHaveBeenCalledWith('b');
  });

  test('ArrowRight skips disabled tabs', () => {
    const onChange = vi.fn();
    const { container } = render(
      <Tabs tabs={TABS} activeTab="b" onChange={onChange} panels={PANELS} />,
    );
    const tablist = container.querySelector('[role="tablist"]') as HTMLElement;
    fireEvent.keyDown(tablist, { key: 'ArrowRight' });
    // 'b' -> next enabled is 'd' (c is disabled)
    expect(onChange).toHaveBeenCalledWith('d');
  });

  test('ArrowLeft wraps to last enabled tab from first', () => {
    const onChange = vi.fn();
    const { container } = render(
      <Tabs tabs={TABS} activeTab="a" onChange={onChange} panels={PANELS} />,
    );
    const tablist = container.querySelector('[role="tablist"]') as HTMLElement;
    fireEvent.keyDown(tablist, { key: 'ArrowLeft' });
    expect(onChange).toHaveBeenCalledWith('d');
  });

  test('Home jumps to first enabled', () => {
    const onChange = vi.fn();
    const { container } = render(
      <Tabs tabs={TABS} activeTab="d" onChange={onChange} panels={PANELS} />,
    );
    fireEvent.keyDown(container.querySelector('[role="tablist"]') as HTMLElement, { key: 'Home' });
    expect(onChange).toHaveBeenCalledWith('a');
  });

  test('End jumps to last enabled', () => {
    const onChange = vi.fn();
    const { container } = render(
      <Tabs tabs={TABS} activeTab="a" onChange={onChange} panels={PANELS} />,
    );
    fireEvent.keyDown(container.querySelector('[role="tablist"]') as HTMLElement, { key: 'End' });
    expect(onChange).toHaveBeenCalledWith('d');
  });

  test('badge renders alongside the label', () => {
    const tabs: TabSpec[] = [{ id: 'a', label: 'A', badge: <span>3</span> }];
    const { getByText } = render(
      <Tabs tabs={tabs} activeTab="a" onChange={() => {}} panels={{ a: <div /> }} />,
    );
    expect(getByText('3')).toBeInTheDocument();
  });
});
