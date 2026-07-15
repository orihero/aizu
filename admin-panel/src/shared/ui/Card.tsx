import type { ReactNode } from 'react';
import { cn } from '@/shared/lib/cn';
import { useRipple } from '@/shared/hooks/useRipple';

interface CardProps {
  readonly children: ReactNode;
  readonly className?: string;
  /** Dashboard-tile extras: click ripple + overflow clipping for the ink. */
  readonly effects?: boolean;
  /** Optional DOM id so the card can act as a scroll/deep-link anchor. */
  readonly id?: string | undefined;
}

export function Card({ children, className, effects = false, id }: CardProps) {
  const ripple = useRipple();
  return (
    <div
      id={id}
      onPointerDown={effects ? ripple : undefined}
      className={cn(
        'reveal shimmer-on relative rounded-tile border border-border bg-surface shadow-tile',
        'transition-[transform,box-shadow] duration-300 hover:-translate-y-0.5 hover:shadow-pop hover:ring-1 hover:ring-brand/20',
        effects && 'overflow-hidden',
        className,
      )}
    >
      {children}
    </div>
  );
}

interface CardHeaderProps {
  readonly title: ReactNode;
  readonly subtitle?: ReactNode;
  readonly actions?: ReactNode;
}

export function CardHeader({ title, subtitle, actions }: CardHeaderProps) {
  return (
    <div className="flex items-start justify-between gap-3 border-b border-border px-5 py-4">
      <div>
        <div className="flex items-center gap-2 text-[13px] font-semibold">{title}</div>
        {subtitle ? <div className="mt-0.5 text-xs text-text-muted">{subtitle}</div> : null}
      </div>
      {actions ? <div className="shrink-0">{actions}</div> : null}
    </div>
  );
}

export function CardBody({ children, className }: CardProps) {
  return <div className={cn('px-5 py-4', className)}>{children}</div>;
}
