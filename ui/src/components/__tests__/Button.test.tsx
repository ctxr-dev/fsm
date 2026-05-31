import { describe, it, expect, vi } from 'vitest';
import { render, fireEvent } from '@testing-library/preact';
import { Button } from '../Button';

describe('Button', () => {
  it('disabled prop blocks clicks', () => {
    const onClick = vi.fn();
    const { getByRole } = render(
      <Button variant="primary" disabled onClick={onClick}>Go</Button>,
    );
    const btn = getByRole('button') as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
    fireEvent.click(btn);
    expect(onClick).not.toHaveBeenCalled();
  });

  it('loading shows spinner and sets aria-busy', () => {
    const onClick = vi.fn();
    const { getByRole, container } = render(
      <Button variant="primary" loading onClick={onClick}>Save</Button>,
    );
    const btn = getByRole('button') as HTMLButtonElement;
    expect(btn.getAttribute('aria-busy')).toBe('true');
    expect(container.querySelector('[role="status"]')).not.toBeNull();
    fireEvent.click(btn);
    expect(onClick).not.toHaveBeenCalled();
  });

  it('includes the focus ring class so the ring is visible on keyboard focus', () => {
    const { getByRole } = render(<Button variant="primary">Tab</Button>);
    const btn = getByRole('button');
    expect(btn.className).toContain('focus-visible:ring-2');
    expect(btn.className).toContain('focus-visible:ring-emerald-500');
  });

  it('fires onClick when enabled', () => {
    const onClick = vi.fn();
    const { getByRole } = render(
      <Button variant="primary" onClick={onClick}>Go</Button>,
    );
    fireEvent.click(getByRole('button'));
    expect(onClick).toHaveBeenCalledOnce();
  });
});
