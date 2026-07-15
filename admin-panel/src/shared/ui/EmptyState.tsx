import type { ReactNode } from 'react';
import type { LucideIcon } from 'lucide-react';

interface EmptyStateProps {
  readonly icon: LucideIcon;
  readonly title: string;
  readonly description?: string;
  readonly action?: ReactNode;
}

export function EmptyState({ icon: Icon, title, description, action }: EmptyStateProps) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 py-12 text-center">
      <span className="flex size-14 items-center justify-center rounded-full bg-surface-2">
        <Icon className="size-6 text-text-faint" aria-hidden />
      </span>
      <div className="font-head text-base font-semibold text-text">{title}</div>
      {description ? <div className="max-w-sm text-sm text-text-muted">{description}</div> : null}
      {action ? <div className="mt-1">{action}</div> : null}
    </div>
  );
}
