import { useEffect, useState } from 'react';
import { Button } from '@/shared/ui/Button';
import { Modal } from '@/shared/ui/Modal';
import { LEAD_STATUS_LABEL } from '@/shared/selectors/leads';
import type { MatchStatus } from '@/shared/types/domain';

interface BulkReasonModalProps {
  /** The terminal status the selection is moving into, or null when closed. */
  readonly target: MatchStatus | null;
  /** How many leads the one shared reason will be applied to. */
  readonly count: number;
  readonly onCancel: () => void;
  readonly onConfirm: (reason: string) => void;
  readonly isSubmitting: boolean;
}

/**
 * Captures ONE shared reason before bulk-moving the selected leads into a
 * terminal status (Closed / Couldn't Connect / Archived). The engine requires a
 * reason for those transitions and records it on every affected lead's audit
 * row. Cancel aborts the whole bulk action — nothing has mutated.
 */
export function BulkReasonModal({ target, count, onCancel, onConfirm, isSubmitting }: BulkReasonModalProps) {
  const [reason, setReason] = useState('');
  // Reset the field each time a new bulk move is requested.
  useEffect(() => { setReason(''); }, [target]);

  const trimmed = reason.trim();
  const submit = () => { if (trimmed) onConfirm(trimmed); };
  const leadWord = count === 1 ? 'lead' : 'leads';

  return (
    <Modal
      isOpen={target !== null}
      onClose={onCancel}
      title={target ? `Move ${count} ${leadWord} to ${LEAD_STATUS_LABEL[target]}` : ''}
      footer={
        <>
          <Button type="button" variant="ghost" onClick={onCancel}>
            Cancel
          </Button>
          <Button type="button" variant="default" onClick={submit} disabled={trimmed === '' || isSubmitting}>
            {target ? `Move ${count} ${leadWord}` : 'Confirm'}
          </Button>
        </>
      }
    >
      {target ? (
        <>
          <p className="mb-2 text-xs text-text-muted">
            Moving{' '}
            <span className="font-semibold text-text">
              {count} {leadWord}
            </span>{' '}
            to <span className="font-semibold text-text">{LEAD_STATUS_LABEL[target]}</span>. Add a
            reason — it's recorded on each lead's history.
          </p>
          <textarea
            value={reason}
            onChange={(e) => { setReason(e.target.value); }}
            onKeyDown={(e) => {
              // Cmd/Ctrl+Enter submits; plain Enter stays a newline.
              if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') submit();
            }}
            rows={3}
            placeholder="Why are these leads being moved here?"
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
