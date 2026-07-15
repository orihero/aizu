import { useDroppable } from '@dnd-kit/core';
import { LEAD_STATUS_LABEL, LEAD_STATUS_TONE, type BoardColumn } from '@/shared/selectors/leads';
import { cn } from '@/shared/lib/cn';
import type { Match, MatchStatus } from '@/shared/types/domain';
import { LeadCard } from './LeadCard';

interface LeadColumnProps {
  readonly column: BoardColumn;
  readonly threshold: number;
  readonly onOpen: (lead: Match) => void;
  readonly draggable: boolean;
}

// A thin accent strip per status, keyed to the shared tone token. Kept as a fixed
// fill (not a theme-inverting text token) so it reads the same in both themes.
const ACCENT: Readonly<Record<MatchStatus, string>> = {
  new: 'bg-info',
  in_progress: 'bg-warn',
  interested: 'bg-cloud',
  closed: 'bg-success',
  couldnt_connect: 'bg-danger',
  archived: 'bg-text-faint',
};

export function LeadColumn({ column, threshold, onOpen, draggable }: LeadColumnProps) {
  const { setNodeRef, isOver } = useDroppable({ id: column.status });
  void LEAD_STATUS_TONE; // tone is consumed by the pill; accent strip uses ACCENT.

  return (
    <section
      ref={setNodeRef}
      className={cn(
        'flex w-72 shrink-0 flex-col rounded-tile border border-border bg-surface-2/40 p-2.5 transition',
        isOver && 'ring-2 ring-brand/50',
      )}
      aria-label={`${LEAD_STATUS_LABEL[column.status]} column`}
    >
      <header className="mb-2 flex items-center gap-2 px-1">
        <span className={cn('size-2 rounded-full', ACCENT[column.status])} aria-hidden />
        <h3 className="text-xs font-bold text-text">{LEAD_STATUS_LABEL[column.status]}</h3>
        <span className="ml-auto text-[11px] font-semibold tabular-nums text-text-faint">
          {column.leads.length}
        </span>
      </header>

      <div className="flex min-h-24 grow flex-col gap-2">
        {column.leads.length === 0 ? (
          <p className="rounded-card border border-dashed border-border px-2 py-6 text-center text-[11px] text-text-faint">
            Drop here
          </p>
        ) : (
          column.leads.map((lead) => (
            <LeadCard
              key={lead.commentId}
              lead={lead}
              threshold={threshold}
              onOpen={onOpen}
              draggable={draggable}
            />
          ))
        )}
      </div>
    </section>
  );
}
