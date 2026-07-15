import type { DashboardPeriodKey } from '@/shared/types/domain';
import { cn } from '@/shared/lib/cn';

interface PeriodSelectorProps {
  readonly value: DashboardPeriodKey;
  readonly onChange: (period: DashboardPeriodKey) => void;
}

const OPTIONS: readonly { readonly key: DashboardPeriodKey; readonly label: string }[] = [
  { key: 'today', label: 'Today' },
  { key: 'week', label: 'Week' },
  { key: 'month', label: 'Month' },
];

/** Segmented pill control for the active dashboard period. */
export function PeriodSelector({ value, onChange }: PeriodSelectorProps) {
  return (
    <div className="inline-flex items-center gap-1 rounded-full border border-border p-1">
      {OPTIONS.map((option) => {
        const isActive = option.key === value;
        return (
          <button
            key={option.key}
            type="button"
            aria-pressed={isActive}
            onClick={() => { onChange(option.key); }}
            className={cn(
              'rounded-full px-3.5 py-1 text-[12.5px] font-semibold transition',
              isActive
                ? 'bg-accent text-accent-ink'
                : 'text-text-muted hover:bg-surface-2',
            )}
          >
            {option.label}
          </button>
        );
      })}
    </div>
  );
}
