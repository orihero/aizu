import type { ReactNode } from 'react';

interface PageHeaderProps {
  readonly title: string;
  readonly subtitle?: string;
  /** Right-aligned slot for controls (e.g. a period selector or primary action). */
  readonly actions?: ReactNode;
}

/** Per-page header. Pages render their own so they can supply a right-slot. */
export function PageHeader({ title, subtitle, actions }: PageHeaderProps) {
  return (
    <header className="mb-6 flex flex-wrap items-end justify-between gap-4">
      <div className="min-w-0">
        <h1 className="font-head text-[30px] font-extrabold leading-none tracking-tight">
          {title}
        </h1>
        {subtitle ? (
          <p className="mt-2 text-[13.5px] font-medium text-text-muted">{subtitle}</p>
        ) : null}
      </div>
      {actions ? <div className="flex items-center gap-2">{actions}</div> : null}
    </header>
  );
}
