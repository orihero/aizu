import { useEffect, useState } from 'react';
import { Button } from '@/shared/ui/Button';
import { Modal } from '@/shared/ui/Modal';
import { LEAD_STATUS_LABEL } from '@/shared/selectors/leads';
import type { Match, MatchStatus } from '@/shared/types/domain';

export interface PendingMove {
  readonly lead: Match;
  readonly target: MatchStatus;
}

interface ReasonMoveModalProps {
  readonly pending: PendingMove | null;
  readonly onCancel: () => void;
  readonly onConfirm: (reason: string) => void;
  readonly isSubmitting: boolean;
}

/**
 * Blocks a drop into a terminal column (Closed / Couldn't Connect / Archived)
 * until the operator writes a non-empty reason. Cancel aborts the move entirely
 * — the board never mutated, so nothing to roll back.
 */
export function ReasonMoveModal({ pending, onCancel, onConfirm, isSubmitting }: ReasonMoveModalProps) {
  const [reason, setReason] = useState('');
  // Reset the field each time a new move is requested.
  useEffect(() => { setReason(''); }, [pending]);

  const trimmed = reason.trim();
  const submit = () => { if (trimmed) onConfirm(trimmed); };

  return (
    <Modal
      isOpen={pending !== null}
      onClose={onCancel}
      title={pending ? `Move to ${LEAD_STATUS_LABEL[pending.target]}` : ''}
      footer={
        <>
          <Button type="button" variant="ghost" onClick={onCancel}>
            Cancel
          </Button>
          <Button type="button" variant="default" onClick={submit} disabled={trimmed === '' || isSubmitting}>
            Confirm move
          </Button>
        </>
      }
    >
      {pending ? (
        <>
          <p className="mb-2 text-xs text-text-muted">
            Moving <span className="font-semibold text-text">{pending.lead.username}</span> to{' '}
            <span className="font-semibold text-text">{LEAD_STATUS_LABEL[pending.target]}</span>. Add a
            reason — it's recorded on the lead's history.
          </p>
          <textarea
            value={reason}
            onChange={(e) => { setReason(e.target.value); }}
            onKeyDown={(e) => {
              // Cmd/Ctrl+Enter submits; plain Enter stays a newline.
              if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') submit();
            }}
            rows={3}
            placeholder="Why is this lead being moved here?"
            className="w-full resize-none rounded-card border border-border bg-surface px-3 py-2 text-xs outline-none transition-colors focus:border-brand/50"
          />
          {trimmed === '' ? (
            <p role="alert" className="mt-1 text-[11px] text-text-faint">
              A reason is required for this status.
            </p>
          ) : null}
        </>
      ) : null}
    </Modal>
  );
}
