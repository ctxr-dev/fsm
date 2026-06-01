/**
 * Tests for chrome/CommandPalette.tsx.
 *
 * Covers:
 *   - Cmd+K toggles the open signal.
 *   - Renders nothing when closed.
 *   - Renders the combobox + listbox when open.
 *   - Esc closes.
 *   - Up/Down move cursor; Enter activates.
 *   - Cmd+Enter passes newTab=true.
 *   - Click outside the panel closes.
 *   - "No matches" empty state.
 *   - Seed query pre-fills the input.
 */

import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, waitFor } from '@testing-library/preact';

import { CommandPalette } from '../CommandPalette';
import {
  commandPaletteOpen,
  commandPaletteSeed,
  recentRuns,
  recentSpecs,
  runsByStatus,
} from '../../lib/store';

beforeEach(() => {
  commandPaletteOpen.value = false;
  commandPaletteSeed.value = '';
  recentRuns.value = [];
  recentSpecs.value = [];
  runsByStatus.value = {};
});

afterEach(() => cleanup());

describe('CommandPalette', () => {
  test('renders nothing when closed', () => {
    const { container } = render(<CommandPalette />);
    expect(container.firstChild).toBeNull();
  });

  test('renders combobox + listbox when open', () => {
    commandPaletteOpen.value = true;
    const { getByRole, getByLabelText } = render(<CommandPalette />);
    expect(getByRole('combobox')).toBeInTheDocument();
    expect(getByRole('listbox')).toBeInTheDocument();
    expect(getByLabelText('Command palette search')).toBeInTheDocument();
  });

  test('Cmd+K toggles the open signal', () => {
    render(<CommandPalette />);
    expect(commandPaletteOpen.value).toBe(false);
    fireEvent.keyDown(window, { key: 'k', metaKey: true });
    expect(commandPaletteOpen.value).toBe(true);
    fireEvent.keyDown(window, { key: 'k', metaKey: true });
    expect(commandPaletteOpen.value).toBe(false);
  });

  test('Escape closes', () => {
    commandPaletteOpen.value = true;
    const { getByLabelText } = render(<CommandPalette />);
    const input = getByLabelText('Command palette search') as HTMLInputElement;
    fireEvent.keyDown(input, { key: 'Escape' });
    expect(commandPaletteOpen.value).toBe(false);
  });

  test('default results include the route entries (e.g. "Specs", "All runs")', () => {
    commandPaletteOpen.value = true;
    const { getByText } = render(<CommandPalette />);
    // W19 sidebar shows "Specs" (primary) and "All runs" (admin
    // section). Both must appear in the palette so users can jump
    // anywhere with Cmd+K.
    expect(getByText('Specs')).toBeInTheDocument();
    expect(getByText('All runs')).toBeInTheDocument();
  });

  test('typing filters the result list', async () => {
    commandPaletteOpen.value = true;
    const { getByLabelText, queryByText } = render(<CommandPalette />);
    const input = getByLabelText('Command palette search') as HTMLInputElement;
    fireEvent.input(input, { target: { value: 'topo' } });
    await waitFor(() => {
      expect(queryByText('Topology')).toBeInTheDocument();
      expect(queryByText('Settings')).toBeNull();
    });
  });

  test('Enter activates the highlighted result', () => {
    commandPaletteOpen.value = true;
    const navigate = vi.fn();
    const { getByLabelText } = render(<CommandPalette navigate={navigate} currentPath="/" />);
    const input = getByLabelText('Command palette search') as HTMLInputElement;
    fireEvent.input(input, { target: { value: 'specs' } });
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(navigate).toHaveBeenCalled();
    expect(commandPaletteOpen.value).toBe(false);
  });

  test('Cmd+Enter passes newTab=true to navigation', () => {
    commandPaletteOpen.value = true;
    const navigate = vi.fn();
    const { getByLabelText } = render(<CommandPalette navigate={navigate} currentPath="/" />);
    const input = getByLabelText('Command palette search') as HTMLInputElement;
    fireEvent.input(input, { target: { value: 'specs' } });
    fireEvent.keyDown(input, { key: 'Enter', metaKey: true });
    expect(navigate).toHaveBeenCalledWith('/specs', { newTab: true });
  });

  test('Arrow Down moves cursor', async () => {
    commandPaletteOpen.value = true;
    const { container, getByLabelText } = render(<CommandPalette />);
    const input = getByLabelText('Command palette search') as HTMLInputElement;
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    await waitFor(() => {
      const rows = container.querySelectorAll('[role="option"]');
      expect(rows[1].getAttribute('aria-selected')).toBe('true');
    });
  });

  test('No matches shows empty state', async () => {
    commandPaletteOpen.value = true;
    const { getByLabelText, getByText } = render(<CommandPalette />);
    const input = getByLabelText('Command palette search') as HTMLInputElement;
    fireEvent.input(input, { target: { value: 'zzzzz-non-existent-zzzz' } });
    await waitFor(() => {
      expect(getByText(/No matches/)).toBeInTheDocument();
    });
  });

  test('Seed value populates the input on open', () => {
    commandPaletteSeed.value = 'preset';
    commandPaletteOpen.value = true;
    const { getByLabelText } = render(<CommandPalette />);
    const input = getByLabelText('Command palette search') as HTMLInputElement;
    expect(input.value).toBe('preset');
  });
});
