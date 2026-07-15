import type { DashboardPeriodKey } from '@/shared/types/domain';
import { cn } from '@/shared/lib/cn';

interface ReportsPeriodSelectorProps {
  readonly value: DashboardPeriodKey;
  readonly onChange: (period: DashboardPeriodKey) => void;
}

const OPTIONS: readonly { readonly key: DashboardPeriodKey; readonly label: string }[] = [
  { key: 'today', label: 'Today' },
  { key: 'week', label: '7 days' },
  { key: 'month', label: '30 days' },
];

/** Segmented pill control for the active report period (local to Reports). */
export function ReportsPeriodSelector({ value, onChange }: ReportsPeriodSelectorProps) {
  return (
    <div className="inline-flex items-center gap-1 rounded-full border border-border p-1">
      {OPTIONS.map((option) => {
        const isActive = option.key === value;
        return (
          <button
            key={option.key}
            type="button"
            aria-pressed={isActive}
            onClick={() => {
              onChange(option.key);
            }}
            className={cn(
              'rounded-full px-3.5 py-1 text-[12.5px] font-semibold transition',
              isActive ? 'bg-accent text-accent-ink' : 'text-text-muted hover:bg-surface-2',
            )}
          >
            {option.label}
          </button>
        );
      })}
    </div>
  );
}
