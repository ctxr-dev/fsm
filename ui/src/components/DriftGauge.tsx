/**
 * Visual gauge for a drift score in the ``[0, 1]`` band.
 *
 * Anything >= 0.7 is rendered in danger red so an operator can spot
 * a degenerating run at a glance; ``[0.4, 0.7)`` is amber and the
 * remainder is the calm emerald accent. The bar honours
 * ``prefers-reduced-motion`` because Tailwind's ``motion-safe``
 * variant gates the transition on the media query.
 */

import type { JSX } from 'preact';

export interface DriftGaugeProps {
  score: number;
}

export function DriftGauge({ score }: DriftGaugeProps): JSX.Element {
  const clamped = Math.max(0, Math.min(1, score));
  const pct = Math.round(clamped * 100);
  const tone =
    clamped >= 0.7
      ? 'bg-red-500'
      : clamped >= 0.4
      ? 'bg-amber-500'
      : 'bg-emerald-500';
  return (
    <div class="space-y-1">
      <div class="flex items-baseline justify-between text-xs text-slate-500 dark:text-slate-400">
        <span>Drift score</span>
        <span class="font-mono text-slate-700 dark:text-slate-200">
          {clamped.toFixed(3)}
        </span>
      </div>
      <div
        class="h-2 w-full rounded-full bg-slate-200 dark:bg-slate-700 overflow-hidden"
        role="progressbar"
        aria-label="Drift score"
        aria-valuemin={0}
        aria-valuemax={1}
        aria-valuenow={clamped}
      >
        <div
          class={`h-full ${tone} motion-safe:transition-[width] motion-safe:duration-300`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}
