/**
 * Tests for chrome/TopBarExtras.tsx.
 */

import { afterEach, beforeEach, describe, expect, test } from 'vitest';
import { cleanup, fireEvent, render } from '@testing-library/preact';

import { TopBarExtras } from '../TopBarExtras';
import {
  commandPaletteOpen,
  commandPaletteSeed,
  densityMode,
  keyboardHelpOpen,
  notifications,
  notificationCentreOpen,
  pushNotification,
  theme,
} from '../../lib/store';

beforeEach(() => {
  commandPaletteOpen.value = false;
  commandPaletteSeed.value = '';
  keyboardHelpOpen.value = false;
  notificationCentreOpen.value = false;
  notifications.value = [];
  densityMode.value = 'comfortable';
  theme.value = 'auto';
});

afterEach(() => cleanup());

describe('TopBarExtras', () => {
  test('renders all five buttons', () => {
    const { getByLabelText } = render(<TopBarExtras />);
    expect(getByLabelText(/Open command palette/)).toBeInTheDocument();
    expect(getByLabelText(/Density/)).toBeInTheDocument();
    expect(getByLabelText(/Theme/)).toBeInTheDocument();
    expect(getByLabelText(/Notifications/)).toBeInTheDocument();
    expect(getByLabelText(/Keyboard shortcuts/)).toBeInTheDocument();
  });

  test('Search button opens the palette + clears seed', () => {
    commandPaletteSeed.value = 'leftover';
    const { getByLabelText } = render(<TopBarExtras />);
    fireEvent.click(getByLabelText(/Open command palette/));
    expect(commandPaletteOpen.value).toBe(true);
    expect(commandPaletteSeed.value).toBe('');
  });

  test('Density button cycles compact → comfortable → spacious → compact', () => {
    densityMode.value = 'comfortable';
    const { getByLabelText } = render(<TopBarExtras />);
    fireEvent.click(getByLabelText(/Density/));
    expect(densityMode.value).toBe('spacious');
    fireEvent.click(getByLabelText(/Density/));
    expect(densityMode.value).toBe('compact');
    fireEvent.click(getByLabelText(/Density/));
    expect(densityMode.value).toBe('comfortable');
  });

  test('Theme button cycles auto → light → dark → auto', () => {
    theme.value = 'auto';
    const { getByLabelText } = render(<TopBarExtras />);
    fireEvent.click(getByLabelText(/Theme/));
    expect(theme.value).toBe('light');
    fireEvent.click(getByLabelText(/Theme/));
    expect(theme.value).toBe('dark');
    fireEvent.click(getByLabelText(/Theme/));
    expect(theme.value).toBe('auto');
  });

  test('Notifications button toggles centre open signal', () => {
    const { getByLabelText } = render(<TopBarExtras />);
    fireEvent.click(getByLabelText(/Notifications/));
    expect(notificationCentreOpen.value).toBe(true);
    fireEvent.click(getByLabelText(/Notifications/));
    expect(notificationCentreOpen.value).toBe(false);
  });

  test('Unread badge shows count when >0 (capped at 9+)', () => {
    pushNotification({ id: '1', kind: 'k', title: 't', timestamp: 't', read: false });
    pushNotification({ id: '2', kind: 'k', title: 't', timestamp: 't', read: false });
    const { container } = render(<TopBarExtras />);
    const badge = container.querySelector('button[aria-label^="Notifications"] span[aria-hidden]');
    expect(badge?.textContent).toBe('2');
  });

  test('Unread badge displays 9+ when count exceeds 9', () => {
    for (let i = 0; i < 15; i++) {
      pushNotification({ id: String(i), kind: 'k', title: 't', timestamp: 't', read: false });
    }
    const { container } = render(<TopBarExtras />);
    const badge = container.querySelector('button[aria-label^="Notifications"] span[aria-hidden]');
    expect(badge?.textContent).toBe('9+');
  });

  test('No badge renders when zero unread', () => {
    const { container } = render(<TopBarExtras />);
    const badge = container.querySelector('button[aria-label^="Notifications"] span[aria-hidden]');
    expect(badge).toBeNull();
  });

  test('Help button opens the keyboard help', () => {
    const { getByLabelText } = render(<TopBarExtras />);
    fireEvent.click(getByLabelText(/Keyboard shortcuts/));
    expect(keyboardHelpOpen.value).toBe(true);
  });
});
