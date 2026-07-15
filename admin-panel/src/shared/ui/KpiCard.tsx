import type { ReactNode } from 'react';
import type { LucideIcon } from 'lucide-react';
import { Card } from './Card';

interface KpiCardProps {
  readonly icon: LucideIcon;
  readonly label: string;
  readonly value: ReactNode;
  readonly meta?: ReactNode;
}

export function KpiCard({ icon: Icon, label, value, meta }: KpiCardProps) {
  return (
    <Card className="px-5 py-5">
      <div className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wide text-text-faint">
        <Icon className="size-3.5" aria-hidden />
        {label}
      </div>
      <div className="mt-3 font-head text-[30px] font-extrabold tabular tracking-tight leading-none">
        {value}
      </div>
      {meta ? <div className="mt-2 text-[11.5px] text-text-faint">{meta}</div> : null}
    </Card>
  );
}
