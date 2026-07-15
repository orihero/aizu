import { cn } from '@/shared/lib/cn';
import { formatScore } from '@/shared/lib/formatters';

interface ScorePillProps {
  readonly score: number;
  readonly threshold: number;
}

const STRONG_MARGIN = 0.15;

export function ScorePill({ score, threshold }: ScorePillProps) {
  const isBelow = score < threshold;
  const isStrong = score >= threshold + STRONG_MARGIN;
  const fillFraction = Math.min(1, Math.max(0, score));
  return (
    <span
      className={cn(
        'inline-flex items-center gap-2 rounded-full px-2.5 py-1 font-head text-xs font-bold tabular',
        isBelow
          ? 'bg-warn-soft text-warn'
          : isStrong
            ? 'bg-success-soft text-success'
            : 'bg-surface-2 text-text',
      )}
    >
      <span className="relative inline-block h-1.5 w-10 overflow-hidden rounded-full bg-border" aria-hidden>
        <span
          className={cn(
            'absolute inset-y-0 left-0 rounded-full',
            isBelow ? 'bg-warn' : isStrong ? 'bg-success' : 'bg-text-muted',
          )}
          style={{ width: `${Math.round(fillFraction * 100)}%` }}
        />
      </span>
      {formatScore(score)}
    </span>
  );
}
