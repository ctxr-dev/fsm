/**
 * Tests for chrome/NotificationCentre.tsx.
 */

import { afterEach, beforeEach, describe, expect, test } from 'vitest';
import { cleanup, fireEvent, render } from '@testing-library/preact';

import { NotificationCentre } from '../NotificationCentre';
import {
  notifications,
  notificationCentreOpen,
  pushNotification,
} from '../../lib/store';

beforeEach(() => {
  notifications.value = [];
  notificationCentreOpen.value = false;
});

afterEach(() => cleanup());

describe('NotificationCentre', () => {
  test('renders nothing when closed', () => {
    const { container } = render(<NotificationCentre />);
    expect(container.firstChild).toBeNull();
  });

  test('renders an empty state when there are no notifications', () => {
    notificationCentreOpen.value = true;
    const { getByText } = render(<NotificationCentre />);
    expect(getByText(/No notifications/)).toBeInTheDocument();
  });

  test('renders each notification as a list item', () => {
    pushNotification({
      id: 'n1',
      kind: 'run_completed',
      title: 'Run X completed',
      timestamp: '2026-06-01T00:00:00Z',
      read: false,
    });
    pushNotification({
      id: 'n2',
      kind: 'drift_threshold_breached',
      title: 'Drift on run Y',
      body: 'Score 0.85',
      runId: 'y',
      timestamp: '2026-06-01T00:01:00Z',
      read: false,
    });
    notificationCentreOpen.value = true;
    const { container, getByText } = render(<NotificationCentre />);
    expect(container.querySelectorAll('li').length).toBe(2);
    expect(getByText('Run X completed')).toBeInTheDocument();
    expect(getByText('Drift on run Y')).toBeInTheDocument();
  });

  test('shows the kind + relative timestamp', () => {
    pushNotification({
      id: 'n1',
      kind: 'spec_registered',
      title: 'New spec',
      timestamp: '2026-06-01T00:00:00Z',
      read: false,
    });
    notificationCentreOpen.value = true;
    const { getByText } = render(<NotificationCentre />);
    expect(getByText('spec_registered')).toBeInTheDocument();
  });

  test('closing the centre marks all notifications as read', () => {
    pushNotification({ id: 'n1', kind: 'k', title: 't', timestamp: 't', read: false });
    pushNotification({ id: 'n2', kind: 'k', title: 't', timestamp: 't', read: false });
    notificationCentreOpen.value = true;
    const { getByLabelText } = render(<NotificationCentre />);
    fireEvent.click(getByLabelText('Close sheet'));
    expect(notifications.value.every((n) => n.read)).toBe(true);
  });

  test('per-row "Open run →" link only renders when runId is present', () => {
    pushNotification({ id: '1', kind: 'x', title: 't', runId: 'abc', timestamp: 't', read: false });
    pushNotification({ id: '2', kind: 'x', title: 't', timestamp: 't', read: false });
    notificationCentreOpen.value = true;
    const { container } = render(<NotificationCentre />);
    const links = container.querySelectorAll('a[href^="/runs/"]');
    expect(links.length).toBe(1);
  });
});
