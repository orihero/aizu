import {
  DndContext,
  KeyboardSensor,
  PointerSensor,
  useSensor,
  useSensors,
  type DragEndEvent,
} from '@dnd-kit/core';
import { useMemo } from 'react';
import { useSetMatchStatus } from '@/shared/hooks/useSetMatchStatus';
import { useCan } from '@/shared/hooks/useCan';
import {
  LEAD_STATUS_ORDER,
  isTerminalStatus,
  selectBoardColumns,
} from '@/shared/selectors/leads';
import type { Match, MatchStatus } from '@/shared/types/domain';
import { LeadColumn } from './LeadColumn';

interface LeadBoardProps {
  readonly leads: readonly Match[];
  readonly threshold: number;
  readonly onOpen: (lead: Match) => void;
  /** Opens the reason modal for a drop into a terminal column (deferred commit). */
  readonly onRequestTerminalMove: (lead: Match, target: MatchStatus) => void;
}

function isStatus(value: unknown): value is MatchStatus {
  return typeof value === 'string' && (LEAD_STATUS_ORDER as readonly string[]).includes(value);
}

export function LeadBoard({ leads, threshold, onOpen, onRequestTerminalMove }: LeadBoardProps) {
  const setStatus = useSetMatchStatus();
  const canEdit = useCan('edit_leads');
  const columns = useMemo(() => selectBoardColumns(leads), [leads]);

  // A 5px activation distance lets a plain click reach the card's Open button
  // instead of being captured as a drag.
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } }),
    useSensor(KeyboardSensor),
  );

  const onDragEnd = (event: DragEndEvent) => {
    const target = event.over?.id;
    if (!isStatus(target)) return;
    const lead = leads.find((m) => m.id === event.active.id);
    if (!lead || lead.status === target) return;
    if (isTerminalStatus(target)) {
      // Terminal moves need a reason — defer the write to the modal.
      onRequestTerminalMove(lead, target);
      return;
    }
    setStatus.mutate({
      campaignId: lead.campaignId,
      commentId: lead.commentId,
      platform: lead.platform,
      status: target,
    });
  };

  return (
    <DndContext sensors={sensors} onDragEnd={onDragEnd}>
      <div className="flex gap-3 overflow-x-auto pb-2">
        {columns.map((column) => (
          <LeadColumn
            key={column.status}
            column={column}
            threshold={threshold}
            onOpen={onOpen}
            draggable={canEdit}
          />
        ))}
      </div>
    </DndContext>
  );
}
