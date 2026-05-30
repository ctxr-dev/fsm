import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/preact';
import { Pill, type PillVariant } from '../Pill';

const VARIANT_FRAGMENT: Record<PillVariant, string> = {
  neutral: 'bg-slate-100',
  success: 'bg-emerald-100',
  warning: 'bg-amber-100',
  danger: 'bg-red-100',
  info: 'bg-slate-200',
};

describe('Pill', () => {
  it('renders all variants with the right colour class', () => {
    for (const variant of Object.keys(VARIANT_FRAGMENT) as PillVariant[]) {
      const { container } = render(<Pill variant={variant}>{variant}</Pill>);
      const span = container.querySelector('span');
      expect(span).not.toBeNull();
      expect(span!.className).toContain(VARIANT_FRAGMENT[variant]);
      expect(span!.className).toContain('rounded-full');
    }
  });

  it('applies aria-label override', () => {
    const { container } = render(
      <Pill variant="success" aria-label="ok">done</Pill>,
    );
    expect(container.querySelector('span')!.getAttribute('aria-label')).toBe('ok');
  });

  it('passes through title attribute', () => {
    const { container } = render(
      <Pill variant="info" title="tip">hi</Pill>,
    );
    expect(container.querySelector('span')!.getAttribute('title')).toBe('tip');
  });
});
