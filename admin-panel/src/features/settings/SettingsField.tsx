import type { ReactNode } from 'react';
import { cn } from '@/shared/lib/cn';

/** Shared input styling so every settings control reads identically. */
export const INPUT_CLASS =
  'w-full rounded-lg border border-border bg-surface px-3 py-2 text-sm text-text ' +
  'transition focus:border-accent focus:outline-none focus:ring-2 focus:ring-accent/30 ' +
  'disabled:cursor-not-allowed disabled:opacity-50';

interface FieldProps {
  readonly label: string;
  readonly hint?: string;
  readonly htmlFor?: string;
  readonly children: ReactNode;
  readonly className?: string;
}

/** Label + control + optional hint, stacked. */
export function Field({ label, hint, htmlFor, children, className }: FieldProps) {
  return (
    <div className={cn('flex flex-col gap-1.5', className)}>
      <label htmlFor={htmlFor} className="text-xs font-semibold text-text-muted">
        {label}
      </label>
      {children}
      {hint ? <p className="text-[11px] text-text-faint">{hint}</p> : null}
    </div>
  );
}
