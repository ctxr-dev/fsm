/**
 * Tests for components/Sheet.tsx.
 */

import { afterEach, describe, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, waitFor } from '@testing-library/preact';

import { Sheet } from '../Sheet';

afterEach(() => {
  cleanup();
  document.body.style.overflow = '';
});

describe('Sheet', () => {
  test('renders nothing when open=false', () => {
    const { queryByRole } = render(
      <Sheet open={false} onClose={() => {}} title="X">
        body
      </Sheet>,
    );
    expect(queryByRole('dialog')).toBeNull();
  });

  test('renders a dialog with the title when open', () => {
    const { getByRole, getByText } = render(
      <Sheet open={true} onClose={() => {}} title="My Sheet">
        body
      </Sheet>,
    );
    const dialog = getByRole('dialog');
    expect(dialog).toBeInTheDocument();
    expect(dialog.getAttribute('aria-modal')).toBe('true');
    expect(getByText('My Sheet')).toBeInTheDocument();
  });

  test('Escape closes when not pinned', () => {
    const onClose = vi.fn();
    render(
      <Sheet open={true} onClose={onClose} title="X" pushHistory={false}>
        body
      </Sheet>,
    );
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledOnce();
  });

  test('Escape does NOT close when pinned', () => {
    const onClose = vi.fn();
    render(
      <Sheet open={true} onClose={onClose} title="X" pinned={true} pushHistory={false}>
        body
      </Sheet>,
    );
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).not.toHaveBeenCalled();
  });

  test('backdrop click closes when not fullscreen + closeOnBackdrop true', () => {
    const onClose = vi.fn();
    const { container } = render(
      <Sheet open={true} onClose={onClose} title="X" pushHistory={false}>
        body
      </Sheet>,
    );
    const backdrop = container.firstChild as HTMLElement;
    fireEvent.click(backdrop, { target: backdrop, currentTarget: backdrop });
    expect(onClose).toHaveBeenCalledOnce();
  });

  test('backdrop click does NOT close when closeOnBackdrop=false', () => {
    const onClose = vi.fn();
    const { container } = render(
      <Sheet open={true} onClose={onClose} title="X" closeOnBackdrop={false} pushHistory={false}>
        body
      </Sheet>,
    );
    const backdrop = container.firstChild as HTMLElement;
    fireEvent.click(backdrop, { target: backdrop, currentTarget: backdrop });
    expect(onClose).not.toHaveBeenCalled();
  });

  test('close button fires onClose', () => {
    const onClose = vi.fn();
    const { getByLabelText } = render(
      <Sheet open={true} onClose={onClose} title="X" pushHistory={false}>
        body
      </Sheet>,
    );
    fireEvent.click(getByLabelText('Close sheet'));
    expect(onClose).toHaveBeenCalledOnce();
  });

  test('cycle-width button cycles through right-third → right-half → fullscreen', () => {
    const { getByLabelText, container } = render(
      <Sheet open={true} onClose={() => {}} title="X" width="right-third" pushHistory={false}>
        body
      </Sheet>,
    );
    const button = () => getByLabelText(/Cycle width/);
    const aside = () => container.querySelector('aside') as HTMLElement;
    expect(aside().className).toContain('sm:w-[33vw]');
    fireEvent.click(button());
    expect(aside().className).toContain('sm:w-[50vw]');
    fireEvent.click(button());
    expect(aside().className).toContain('max-w-none');
    fireEvent.click(button());
    expect(aside().className).toContain('sm:w-[33vw]');
  });

  test('body scroll lock applied while open', async () => {
    const { rerender } = render(
      <Sheet open={true} onClose={() => {}} title="X" pushHistory={false}>
        body
      </Sheet>,
    );
    expect(document.body.style.overflow).toBe('hidden');
    rerender(
      <Sheet open={false} onClose={() => {}} title="X" pushHistory={false}>
        body
      </Sheet>,
    );
    await waitFor(() => expect(document.body.style.overflow).toBe(''));
  });

  test('renders an optional footer', () => {
    const { getByText } = render(
      <Sheet open={true} onClose={() => {}} title="X" pushHistory={false} footer={<button>OK</button>}>
        body
      </Sheet>,
    );
    expect(getByText('OK')).toBeInTheDocument();
  });
});
