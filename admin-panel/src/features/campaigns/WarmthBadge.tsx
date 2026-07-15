import { Sparkline } from '@/shared/ui/charts';
import { cn } from '@/shared/lib/cn';
import type { Warmth } from '@/shared/types/domain';

// State → human label + dot color (warming PRD §5.4/§7.5). `throttled` is distinct
// from a low score so the operator knows it's transient rate-limiting.
const STATE_META: Readonly<Record<Warmth['state'], { label: string; dot: string }>> = {
  warming: { label: 'Warming', dot: 'bg-warn' },
  ready: { label: 'Ready', dot: 'bg-brand' },
  full: { label: 'Full volume', dot: 'bg-success' },
  throttled: { label: 'Throttled', dot: 'bg-warn' },
};

const COMPONENT_LABELS: ReadonlyArray<readonly [keyof Warmth['components'], string]> = [
  ['age', 'Age'],
  ['ramp', 'Ramp'],
  ['network', 'Network'],
  ['profile', 'Profile'],
  ['trust', 'Trust'],
];

/**
 * Compact account-warmth readout for a campaign card (warming PRD §7.5/§7.7):
 * score% + a state pill + the trend sparkline. The per-component breakdown rides
 * in the `title` tooltip so the headline stays terse.
 */
export function WarmthBadge({ warmth }: { readonly warmth: Warmth }) {
  const meta = STATE_META[warmth.state];
  const breakdown = COMPONENT_LABELS
    .map(([key, label]) => `${label} ${Math.round(warmth.components[key] * 100)}%`)
    .join(' · ');
  const tooltip = `Warmth ${warmth.score}% (${meta.label}) — gate ${warmth.gateMin}%\n${breakdown}`;

  return (
    <div className="flex items-center gap-2" title={tooltip}>
      <span className="text-[11.5px] font-bold uppercase tracking-wider text-text-faint">Warmth</span>
      <span className="font-head text-[13px] font-bold tabular-nums text-text">{warmth.score}%</span>
      <span className="inline-flex items-center gap-1 text-[11px] font-semibold text-text-muted">
        <span className={cn('size-1.5 rounded-full', meta.dot)} aria-hidden />
        {meta.label}
      </span>
      {warmth.trend.length > 0 ? (
        <span className="ml-auto w-16">
          <Sparkline data={warmth.trend} />
        </span>
      ) : null}
    </div>
  );
}
