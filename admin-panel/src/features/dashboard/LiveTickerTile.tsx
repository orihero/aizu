import { Radio } from 'lucide-react';
import type { TickerEntry } from '@/shared/types/domain';
import { LEAD_INTENT_PLACEHOLDER } from '@/shared/selectors/leads';
import { CardBody, CardHeader } from '@/shared/ui/Card';
import { cn } from '@/shared/lib/cn';
import { formatScore } from '@/shared/lib/formatters';
import { usePrefersReducedMotion } from '@/shared/hooks/usePrefersReducedMotion';
import { platformColor } from '@/shared/ui/charts';

interface LiveTickerTileProps {
  readonly entries: readonly TickerEntry[];
  /** Drives the pulsing live dot; true while a run holds the engine lock. */
  readonly isLive?: boolean;
}

/** Number of rows before the list auto-scrolls as a marquee. */
const MARQUEE_THRESHOLD = 5;

/**
 * Most-recent captured leads, newest first — intent, platform, score, time.
 *
 * v27: the ticker is a customer-facing surface, so it names a lead by what the person
 * WANTS, never by who they are. A `TickerEntry` is not a `Match`, so it cannot use
 * `leadIntentLabel`; it shares the placeholder constant so the copy is identical to
 * the table, the board card and the drawer.
 */
export function LiveTickerTile({ entries, isLive = false }: LiveTickerTileProps) {
  const reduced = usePrefersReducedMotion();
  const marquee = !reduced && entries.length >= MARQUEE_THRESHOLD;
  // Duplicate the rows so the -50% scroll loops seamlessly.
  const rows = marquee ? [...entries, ...entries] : entries;

  return (
    <>
      <CardHeader
        title={
          <>
            <Radio className="size-4 text-text-faint" aria-hidden />
            Live ticker
            {isLive ? <span className="live-dot ml-1.5" aria-label="live" /> : null}
          </>
        }
      />
      <CardBody className="px-0 py-0">
        {entries.length === 0 ? (
          <p className="px-5 py-8 text-center text-sm text-text-muted">No leads yet.</p>
        ) : (
          <div className={cn('ticker-viewport', marquee && 'max-h-[284px] overflow-hidden')}>
            <ul
              className={cn(marquee && 'ticker-track')}
              style={marquee ? ({ '--ticker-dur': `${entries.length * 3}s` } as React.CSSProperties) : undefined}
            >
              {rows.map((entry, i) => (
                <li
                  key={`${entry.id}-${i}`}
                  aria-hidden={marquee && i >= entries.length ? true : undefined}
                  className="flex items-center gap-3 border-b border-border px-5 py-3 last:border-0"
                >
                  <span
                    className="size-2 shrink-0 rounded-full"
                    style={{ background: platformColor(entry.platform, 'var(--color-text-faint)') }}
                    aria-hidden
                  />
                  <div className="min-w-0 flex-1">
                    <div
                      className={cn(
                        'truncate text-[13px] font-medium text-text',
                        entry.intent.trim() === '' && 'font-normal italic text-text-faint',
                      )}
                      title={entry.intent.trim() === '' ? undefined : entry.intent}
                    >
                      {entry.intent.trim() === '' ? LEAD_INTENT_PLACEHOLDER : entry.intent}
                    </div>
                    <div className="text-[11px] text-text-faint">
                      {entry.platform} · {entry.capturedAt.time}
                    </div>
                  </div>
                  <span className="shrink-0 rounded-full bg-surface-2 px-2.5 py-1 font-head text-xs font-bold tabular text-text">
                    {formatScore(entry.score)}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </CardBody>
    </>
  );
}
