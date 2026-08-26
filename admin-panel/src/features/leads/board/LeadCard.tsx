import { useDraggable } from '@dnd-kit/core';
import { Maximize2 } from 'lucide-react';
import { ScorePill } from '@/shared/ui/ScorePill';
import { cn } from '@/shared/lib/cn';
import { LEAD_INTENT_PLACEHOLDER, leadIntentLabel } from '@/shared/selectors/leads';
import type { Match } from '@/shared/types/domain';
import { PlatformChip } from '../PlatformChip';

interface LeadCardProps {
  readonly lead: Match;
  readonly threshold: number;
  readonly onOpen: (lead: Match) => void;
  /** Viewers can read the board but not drag cards between columns. */
  readonly draggable: boolean;
}

/** The newest activity label on a lead (last status change or note), if any. */
function lastActivity(lead: Match): string | null {
  const statusTs = lead.statusHistory.at(-1)?.atTs ?? 0;
  const noteTs = lead.notes.at(-1)?.createdAtTs ?? 0;
  if (statusTs === 0 && noteTs === 0) return null;
  return statusTs >= noteTs ? (lead.statusAt ?? null) : (lead.notes.at(-1)?.createdAt ?? null);
}

export function LeadCard({ lead, threshold, onOpen, draggable }: LeadCardProps) {
  const { attributes, listeners, setNodeRef, isDragging } = useDraggable({
    // The draggable id is the composite lead id, matching every other lead surface.
    id: lead.id,
    disabled: !draggable,
  });
  const activity = lastActivity(lead);
  const noteCount = lead.notes.length;
  // v27: the card is intent-only — no handle, no avatar, no comment, no reveal
  // control. The reveal lives in the drawer, one lead at a time.
  const intent = leadIntentLabel(lead);

  return (
    <div
      ref={setNodeRef}
      {...(draggable ? attributes : {})}
      {...(draggable ? listeners : {})}
      className={cn(
        'rounded-card border border-border bg-surface p-3 shadow-tile transition',
        draggable && 'cursor-grab active:cursor-grabbing',
        isDragging && 'opacity-40',
      )}
    >
      <div className="flex items-start justify-between gap-2">
        <p
          className={cn(
            'min-w-0 line-clamp-3 text-xs font-semibold text-text',
            intent === LEAD_INTENT_PLACEHOLDER && 'font-normal italic text-text-faint',
          )}
        >
          {intent}
        </p>
        <button
          type="button"
          // Distinct accessible name so it never collides with the draggable card.
          aria-label={`Open lead: ${intent}`}
          // Stop the drag sensor from swallowing the click.
          onPointerDown={(e) => { e.stopPropagation(); }}
          onClick={() => { onOpen(lead); }}
          className="flex size-6 shrink-0 items-center justify-center rounded-full text-text-faint transition hover:bg-surface-2 hover:text-text"
        >
          <Maximize2 className="size-3.5" aria-hidden />
        </button>
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        <ScorePill score={lead.score} threshold={threshold} />
        <PlatformChip platform={lead.platform} />
        {noteCount > 0 ? (
          <span className="text-[10px] font-semibold text-text-faint">
            {noteCount} note{noteCount === 1 ? '' : 's'}
          </span>
        ) : null}
      </div>

      {activity ? (
        <p className="mt-1.5 text-[10px] text-text-faint">Last activity {activity}</p>
      ) : null}
    </div>
  );
}
