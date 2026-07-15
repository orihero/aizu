import type { ReactNode } from 'react';
import { Loader2 } from 'lucide-react';
import { Button } from './Button';
import { Modal } from './Modal';

interface ConfirmDialogProps {
  readonly isOpen: boolean;
  readonly title: string;
  readonly message: ReactNode;
  readonly confirmLabel?: string;
  readonly cancelLabel?: string;
  readonly tone?: 'default' | 'danger';
  readonly isPending?: boolean;
  readonly onConfirm: () => void;
  readonly onClose: () => void;
}

/**
 * Small blocking "are you sure?" dialog for a single consequential action
 * (pause, archive). Built on the centered, focus-trapped Modal so Escape and the
 * backdrop cancel; the primary button can show a pending spinner while the
 * underlying mutation is in flight.
 */
export function ConfirmDialog({
  isOpen,
  title,
  message,
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  tone = 'default',
  isPending = false,
  onConfirm,
  onClose,
}: ConfirmDialogProps) {
  const footer = (
    <>
      <Button variant="ghost" onClick={onClose} disabled={isPending}>
        {cancelLabel}
      </Button>
      <Button variant={tone === 'danger' ? 'danger' : 'default'} onClick={onConfirm} disabled={isPending}>
        {isPending ? <Loader2 className="size-3.5 animate-spin" aria-hidden /> : null}
        {confirmLabel}
      </Button>
    </>
  );

  return (
    <Modal isOpen={isOpen} onClose={onClose} title={title} footer={footer}>
      <p className="text-sm leading-relaxed text-text-muted">{message}</p>
    </Modal>
  );
}
