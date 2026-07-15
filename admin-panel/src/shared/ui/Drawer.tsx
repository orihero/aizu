import { useEffect, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { X } from 'lucide-react';

interface DrawerProps {
  readonly isOpen: boolean;
  readonly onClose: () => void;
  readonly title: ReactNode;
  readonly children: ReactNode;
  readonly footer?: ReactNode;
}

export function Drawer({ isOpen, onClose, title, children, footer }: DrawerProps) {
  useEffect(() => {
    if (!isOpen) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', onKeyDown);
    return () => { document.removeEventListener('keydown', onKeyDown); };
  }, [isOpen, onClose]);

  if (!isOpen) return null;

  // Portal to <body> so the fixed-position overlay is anchored to the viewport,
  // never to an ancestor that establishes a containing block (a card holding a
  // `transform`/`filter` from its entrance animation would otherwise trap it).
  return createPortal(
    <>
      {/* Click-away affordance only — Escape and the close button are the
          accessible paths, so the backdrop stays out of the tab order. */}
      <div aria-hidden onClick={onClose} className="fixed inset-0 z-40 bg-[rgb(22_22_26/0.38)]" />
      <aside
        role="dialog"
        aria-modal="true"
        aria-label="Details"
        className="fixed inset-y-3 right-3 z-50 flex w-full max-w-md flex-col overflow-hidden rounded-tile bg-surface shadow-pop"
      >
        <header className="flex items-center justify-between gap-3 px-5 pb-4 pt-5">
          <div className="min-w-0">{title}</div>
          <button
            type="button"
            onClick={onClose}
            className="flex size-8 shrink-0 items-center justify-center rounded-full border border-border text-text-muted transition-colors hover:bg-text hover:text-white"
            aria-label="Close"
          >
            <X className="size-4" aria-hidden />
          </button>
        </header>
        <div className="grow overflow-y-auto px-5 py-4">{children}</div>
        {footer ? (
          <footer className="flex gap-2 border-t border-border px-5 py-4">{footer}</footer>
        ) : null}
      </aside>
    </>,
    document.body,
  );
}
