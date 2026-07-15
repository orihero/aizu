import type { ReactNode } from 'react';
import { cn } from '@/shared/lib/cn';
import { LEAD_STATUS_LABEL, LEAD_STATUS_TONE } from '@/shared/selectors/leads';
import type { MatchStatus } from '@/shared/types/domain';

export type BadgeTone =
  | 'success'
  | 'warn'
  | 'danger'
  | 'info'
  | 'cloud'
  | 'neutral';

const TONE_CLASSES: Readonly<Record<BadgeTone, string>> = {
  success: 'bg-success-soft text-success',
  warn: 'bg-warn-soft text-warn',
  danger: 'bg-danger-soft text-danger',
  info: 'bg-info-soft text-info',
  cloud: 'bg-cloud-soft text-cloud',
  neutral: 'bg-surface-2 text-text-muted',
};

interface BadgeProps {
  readonly tone: BadgeTone;
  readonly children: ReactNode;
  readonly title?: string;
}

export function Badge({ tone, children, title }: BadgeProps) {
  return (
    <span
      title={title}
      className={cn(
        'inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-bold',
        TONE_CLASSES[tone],
      )}
    >
      {children}
    </span>
  );
}

export function StatusBadge({ status }: { readonly status: MatchStatus }) {
  return <Badge tone={LEAD_STATUS_TONE[status]}>{LEAD_STATUS_LABEL[status]}</Badge>;
}

export function LangBadge({ lang }: { readonly lang: string | null }) {
  return <Badge tone="neutral">{(lang ?? '—').toUpperCase()}</Badge>;
}
