import { cn } from '@/shared/lib/cn';

interface DeltaChipProps {
  /** Pre-formatted delta string from the bridge, e.g. "+20%" or "−4%". */
  readonly delta: string;
  /** Render on a colored tile (uses translucent white instead of soft tones). */
  readonly onColor?: boolean;
}

function isNegative(delta: string): boolean {
  return delta.trim().startsWith('-') || delta.trim().startsWith('−');
}

/** Trend pill for a pre-formatted delta. Positive → success, negative → danger. */
export function DeltaChip({ delta, onColor = false }: DeltaChipProps) {
  const negative = isNegative(delta);
  if (onColor) {
    return (
      <span className="inline-flex items-center rounded-full bg-white/20 px-2 py-0.5 text-[11px] font-bold">
        {delta}
      </span>
    );
  }
  return (
    <span
      className={cn(
        'inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-bold',
        negative ? 'bg-danger-soft text-danger' : 'bg-success-soft text-success',
      )}
    >
      {delta}
    </span>
  );
}
