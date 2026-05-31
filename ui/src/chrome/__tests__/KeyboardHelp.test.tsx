/**
 * Tests for chrome/KeyboardHelp.tsx.
 */

import { afterEach, beforeEach, describe, expect, test } from 'vitest';
import { cleanup, fireEvent, render } from '@testing-library/preact';

import { KeyboardHelp } from '../KeyboardHelp';
import { keyboardHelpOpen } from '../../lib/store';

beforeEach(() => {
  keyboardHelpOpen.value = false;
});

afterEach(() => cleanup());

describe('KeyboardHelp', () => {
  test('renders nothing when closed', () => {
    const { container } = render(<KeyboardHelp />);
    expect(container.firstChild).toBeNull();
  });

  test('? toggles the open signal', () => {
    render(<KeyboardHelp />);
    expect(keyboardHelpOpen.value).toBe(false);
    fireEvent.keyDown(window, { key: '?' });
    expect(keyboardHelpOpen.value).toBe(true);
    fireEvent.keyDown(window, { key: '?' });
    expect(keyboardHelpOpen.value).toBe(false);
  });

  test('? typed inside an input does NOT toggle', () => {
    const input = document.createElement('input');
    document.body.appendChild(input);
    input.focus();
    render(<KeyboardHelp />);
    fireEvent.keyDown(input, { key: '?' });
    expect(keyboardHelpOpen.value).toBe(false);
    input.remove();
  });

  test('renders the global shortcut table when open', () => {
    keyboardHelpOpen.value = true;
    const { getByText } = render(<KeyboardHelp />);
    expect(getByText('Cmd/Ctrl+K')).toBeInTheDocument();
    expect(getByText('Open command palette')).toBeInTheDocument();
  });

  test('renders route navigation chords from ROUTES registry', () => {
    keyboardHelpOpen.value = true;
    const { getByText } = render(<KeyboardHelp />);
    // ROUTES registers `g r` for /runs.
    expect(getByText('g r')).toBeInTheDocument();
  });

  test('Esc closes (via Dialog)', () => {
    keyboardHelpOpen.value = true;
    render(<KeyboardHelp />);
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(keyboardHelpOpen.value).toBe(false);
  });
});
