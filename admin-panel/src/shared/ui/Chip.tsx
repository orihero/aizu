import type { ReactNode } from 'react';
import { cn } from '@/shared/lib/cn';

interface ChipProps {
  readonly isActive: boolean;
  readonly onClick: () => void;
  readonly children: ReactNode;
  readonly count?: number;
}

export function Chip({ isActive, onClick, children, count }: ChipProps) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={isActive}
      className={cn(
        'inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-[11.5px] font-bold transition active:scale-95',
        isActive
          ? 'border border-brand/40 bg-brand/15 text-text'
          : 'border border-border bg-surface text-text-muted hover:text-text',
      )}
    >
      {children}
      {count !== undefined ? <span className="tabular-nums text-text-faint">{count}</span> : null}
    </button>
  );
}
