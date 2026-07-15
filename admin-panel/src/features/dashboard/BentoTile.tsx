import type { ReactNode } from 'react';
import { cn } from '@/shared/lib/cn';
import { Card } from '@/shared/ui/Card';
import { useRipple } from '@/shared/hooks/useRipple';

export type BentoTone = 'brand' | 'lime' | 'ink' | 'surface';

const TONE_CLASSES: Readonly<Record<Exclude<BentoTone, 'surface'>, string>> = {
  brand: 'bg-brand text-on-brand',
  lime: 'bg-accent text-accent-ink',
  ink: 'bg-nav text-nav-text',
};

const SPAN_CLASSES: Readonly<Record<number, string>> = {
  1: 'col-span-1',
  2: 'col-span-2',
  3: 'col-span-3',
  4: 'col-span-4',
};

const ROW_SPAN_CLASSES: Readonly<Record<number, string>> = {
  1: 'row-span-1',
  2: 'row-span-2',
};

interface BentoTileProps {
  readonly children: ReactNode;
  readonly tone?: BentoTone;
  /** Grid columns to occupy (1–4). Collapses to 2 below xl. */
  readonly span?: number;
  /** Grid rows to occupy (1–2). */
  readonly rowSpan?: number;
  readonly className?: string;
}

/**
 * One cell of the dashboard bento grid. The 'surface' tone reuses the shared
 * Card chrome; colored tones render a flat rounded panel matching the Pulse
 * hero/accent/ink tiles. Spans collapse to two columns on narrower viewports.
 */
export function BentoTile({
  children,
  tone = 'surface',
  span = 1,
  rowSpan = 1,
  className,
}: BentoTileProps) {
  const ripple = useRipple();
  const spanClass = SPAN_CLASSES[span] ?? SPAN_CLASSES[1];
  const rowSpanClass = ROW_SPAN_CLASSES[rowSpan] ?? ROW_SPAN_CLASSES[1];
  // Wide tiles collapse to the full two-column width below xl so nothing is squeezed.
  const collapse = span >= 2 ? 'max-xl:col-span-2' : '';
  const layout = cn(spanClass, rowSpanClass, collapse);

  if (tone === 'surface') {
    return <Card effects className={cn(layout, className)}>{children}</Card>;
  }

  return (
    <div
      onPointerDown={ripple}
      className={cn(
        'reveal shimmer-on kinetic-ring relative overflow-hidden rounded-tile p-5 shadow-tile',
        'transition-transform duration-300 hover:-translate-y-0.5',
        TONE_CLASSES[tone],
        layout,
        className,
      )}
    >
      {children}
    </div>
  );
}

interface BentoLabelProps {
  readonly children: ReactNode;
  readonly className?: string;
}

/** Shared uppercase eyebrow label used across tiles. */
export function BentoLabel({ children, className }: BentoLabelProps) {
  return (
    <div
      className={cn(
        'text-[11px] font-semibold uppercase tracking-wide opacity-70',
        className,
      )}
    >
      {children}
    </div>
  );
}
