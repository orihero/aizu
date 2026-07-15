import { useEffect, useId, useRef, type ReactNode } from 'react';

interface ModalProps {
  readonly isOpen: boolean;
  readonly onClose: () => void;
  readonly title: ReactNode;
  readonly children: ReactNode;
  readonly footer?: ReactNode;
}

const FOCUSABLE =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * Centered, focus-trapped dialog. Unlike Drawer (a side panel), this is for
 * blocking confirmations that demand input — Escape and backdrop close, focus
 * moves in on open and is restored on close, and Tab is trapped within the panel.
 */
export function Modal({ isOpen, onClose, title, children, footer }: ModalProps) {
  const titleId = useId();
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!isOpen) return;
    const previouslyFocused = document.activeElement as HTMLElement | null;
    const panel = panelRef.current;
    // Focus the first focusable element (the textarea/first control) on open.
    panel?.querySelector<HTMLElement>(FOCUSABLE)?.focus();

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        onClose();
        return;
      }
      if (event.key !== 'Tab' || !panel) return;
      const focusable = Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE));
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (!first || !last) return;
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('keydown', onKeyDown);
      previouslyFocused?.focus();
    };
  }, [isOpen, onClose]);

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center px-4">
      {/* Backdrop: click-away only; Escape + buttons are the accessible paths. */}
      <div aria-hidden onClick={onClose} className="absolute inset-0 bg-[rgb(22_22_26/0.45)]" />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className="relative z-10 w-full max-w-md overflow-hidden rounded-tile bg-surface shadow-pop"
      >
        <header className="px-5 pb-3 pt-5">
          <h2 id={titleId} className="text-sm font-semibold text-text">
            {title}
          </h2>
        </header>
        <div className="px-5 py-2">{children}</div>
        {footer ? (
          <footer className="flex justify-end gap-2 border-t border-border px-5 py-4">{footer}</footer>
        ) : null}
      </div>
    </div>
  );
}
