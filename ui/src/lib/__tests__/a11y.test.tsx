/**
 * Tests for lib/a11y.ts: useFocusTrap, useEscapeToClose, useBodyScrollLock.
 *
 * Strategy: render a synthetic component that consumes each hook,
 * exercise the DOM, assert observable effects (focus moves, key events
 * fire callbacks, body overflow flips). No mocking of Preact internals.
 */

import { render, cleanup, fireEvent, waitFor } from '@testing-library/preact';
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { useRef } from 'preact/hooks';

import {
  FOCUSABLE_SELECTOR,
  useBodyScrollLock,
  useEscapeToClose,
  useFocusTrap,
} from '../a11y';

afterEach(() => {
  cleanup();
  document.body.style.overflow = '';
});

describe('FOCUSABLE_SELECTOR', () => {
  test('matches the canonical focusable elements', () => {
    const dom = document.createElement('div');
    dom.innerHTML = `
      <a href="#x">link</a>
      <button>btn</button>
      <button disabled>btn-disabled</button>
      <input type="text" />
      <input type="hidden" />
      <select><option>x</option></select>
      <textarea></textarea>
      <div tabindex="0">tab-stop</div>
      <div tabindex="-1">no-stop</div>
      <span>plain</span>
    `;
    const focusables = dom.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR);
    const tags = Array.from(focusables).map((el) => el.tagName.toLowerCase());
    expect(tags).toEqual(['a', 'button', 'input', 'select', 'textarea', 'div']);
  });
});

describe('useFocusTrap', () => {
  function Trapped({ active, children }: { active: boolean; children?: any }) {
    const ref = useRef<HTMLDivElement>(null);
    useFocusTrap(ref, active);
    return (
      <div ref={ref} tabIndex={-1}>
        {children}
      </div>
    );
  }

  test('focuses the first focusable child on activation', async () => {
    const { getByText } = render(
      <Trapped active={true}>
        <button>first</button>
        <button>second</button>
      </Trapped>,
    );
    await waitFor(() => {
      expect(document.activeElement).toBe(getByText('first'));
    });
  });

  test('falls back to the panel itself when no children are focusable', async () => {
    const { container } = render(
      <Trapped active={true}>
        <span>no buttons here</span>
      </Trapped>,
    );
    await waitFor(() => {
      expect(document.activeElement).toBe(container.firstChild);
    });
  });

  test('Tab from the last focusable wraps to the first', async () => {
    const { getByText } = render(
      <Trapped active={true}>
        <button>first</button>
        <button>last</button>
      </Trapped>,
    );
    const last = getByText('last') as HTMLButtonElement;
    last.focus();
    expect(document.activeElement).toBe(last);
    fireEvent.keyDown(document, { key: 'Tab' });
    expect(document.activeElement).toBe(getByText('first'));
  });

  test('Shift+Tab from the first focusable wraps to the last', async () => {
    const { getByText } = render(
      <Trapped active={true}>
        <button>first</button>
        <button>last</button>
      </Trapped>,
    );
    const first = getByText('first') as HTMLButtonElement;
    first.focus();
    fireEvent.keyDown(document, { key: 'Tab', shiftKey: true });
    expect(document.activeElement).toBe(getByText('last'));
  });

  test('restores focus to the previously-active element on deactivation', async () => {
    const trigger = document.createElement('button');
    trigger.textContent = 'trigger';
    document.body.appendChild(trigger);
    trigger.focus();
    expect(document.activeElement).toBe(trigger);

    const { rerender, getByText } = render(
      <Trapped active={true}>
        <button>inside</button>
      </Trapped>,
    );
    await waitFor(() => expect(document.activeElement).toBe(getByText('inside')));

    rerender(
      <Trapped active={false}>
        <button>inside</button>
      </Trapped>,
    );

    await waitFor(() => expect(document.activeElement).toBe(trigger));
    trigger.remove();
  });

  test('inactive hook does not steal focus', () => {
    const outside = document.createElement('button');
    document.body.appendChild(outside);
    outside.focus();
    render(
      <Trapped active={false}>
        <button>inside</button>
      </Trapped>,
    );
    expect(document.activeElement).toBe(outside);
    outside.remove();
  });

  test('disabled focusables are skipped in the cycle', () => {
    const { getByText } = render(
      <Trapped active={true}>
        <button>first</button>
        <button disabled>middle</button>
        <button>last</button>
      </Trapped>,
    );
    const last = getByText('last') as HTMLButtonElement;
    last.focus();
    fireEvent.keyDown(document, { key: 'Tab' });
    expect(document.activeElement).toBe(getByText('first'));
  });
});

describe('useEscapeToClose', () => {
  function Closeable({ active, onClose }: { active: boolean; onClose: () => void }) {
    useEscapeToClose(active, onClose);
    return <div>panel</div>;
  }

  test('Escape fires onClose when active', () => {
    const onClose = vi.fn();
    render(<Closeable active={true} onClose={onClose} />);
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledOnce();
  });

  test('Escape is a no-op when inactive', () => {
    const onClose = vi.fn();
    render(<Closeable active={false} onClose={onClose} />);
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).not.toHaveBeenCalled();
  });

  test('other keys do not fire onClose', () => {
    const onClose = vi.fn();
    render(<Closeable active={true} onClose={onClose} />);
    fireEvent.keyDown(document, { key: 'a' });
    fireEvent.keyDown(document, { key: 'Enter' });
    fireEvent.keyDown(document, { key: 'Tab' });
    expect(onClose).not.toHaveBeenCalled();
  });

  test('multiple instances each fire their own onClose', () => {
    const a = vi.fn();
    const b = vi.fn();
    render(
      <>
        <Closeable active={true} onClose={a} />
        <Closeable active={true} onClose={b} />
      </>,
    );
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(a).toHaveBeenCalledOnce();
    expect(b).toHaveBeenCalledOnce();
  });
});

describe('useBodyScrollLock', () => {
  function Locked({ active }: { active: boolean }) {
    useBodyScrollLock(active);
    return <div>x</div>;
  }

  beforeEach(() => {
    document.body.style.overflow = '';
  });

  test('locks body overflow while active', () => {
    render(<Locked active={true} />);
    expect(document.body.style.overflow).toBe('hidden');
  });

  test('does not lock when inactive', () => {
    render(<Locked active={false} />);
    expect(document.body.style.overflow).toBe('');
  });

  test('restores prior overflow value on unmount', () => {
    document.body.style.overflow = 'auto';
    const { unmount } = render(<Locked active={true} />);
    expect(document.body.style.overflow).toBe('hidden');
    unmount();
    expect(document.body.style.overflow).toBe('auto');
  });

  test('toggling active flips the lock', () => {
    const { rerender } = render(<Locked active={false} />);
    expect(document.body.style.overflow).toBe('');
    rerender(<Locked active={true} />);
    expect(document.body.style.overflow).toBe('hidden');
    rerender(<Locked active={false} />);
    expect(document.body.style.overflow).toBe('');
  });
});
