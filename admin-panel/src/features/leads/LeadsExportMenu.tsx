import { useEffect, useRef, useState } from 'react';
import { ChevronDown, Download, FileSpreadsheet, FileText, Loader2, Sheet } from 'lucide-react';
import { cn } from '@/shared/lib/cn';
import type { Match } from '@/shared/types/domain';
import { exportLeads, type LeadExportFormat } from './exportLeads';

interface ExportOption {
  readonly format: LeadExportFormat;
  readonly label: string;
  readonly Icon: typeof FileText;
}

const EXPORT_OPTIONS: readonly ExportOption[] = [
  { format: 'csv', label: 'CSV (.csv)', Icon: FileText },
  { format: 'excel', label: 'Excel (.xls)', Icon: FileSpreadsheet },
  { format: 'pdf', label: 'PDF (.pdf)', Icon: Sheet },
];

type MenuVariant = 'toolbar' | 'bulk';

interface LeadsExportMenuProps {
  /** The leads to export — the filtered set (toolbar) or the selected set (bulk bar). */
  readonly leads: readonly Match[];
  readonly variant?: MenuVariant;
}

const TRIGGER_CLASS: Readonly<Record<MenuVariant, string>> = {
  toolbar:
    'border border-border bg-surface text-text shadow-tile hover:shadow-lift hover:-translate-y-px',
  bulk: 'border border-accent-ink/15 bg-accent-ink/10 text-accent-ink hover:bg-accent-ink/20',
};

/**
 * Export dropdown for the Leads page: CSV, Excel, and PDF of the given leads.
 * Shared by the toolbar (filtered set) and the bulk bar (selected set). PDF is
 * async (lazy-loaded jsPDF); a failed export surfaces an inline message rather
 * than failing silently.
 */
export function LeadsExportMenu({ leads, variant = 'toolbar' }: LeadsExportMenuProps) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState<LeadExportFormat | null>(null);
  const [error, setError] = useState<string | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const disabled = leads.length === 0;

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: MouseEvent) => {
      if (!containerRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('mousedown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open]);

  const runExport = async (format: LeadExportFormat) => {
    setError(null);
    setBusy(format);
    try {
      await exportLeads(format, leads);
      setOpen(false);
    } catch {
      setError(`Couldn't export ${format.toUpperCase()}. Please try again.`);
    } finally {
      setBusy(null);
    }
  };

  return (
    <div ref={containerRef} className="relative">
      <button
        type="button"
        onClick={() => { setOpen((v) => !v); }}
        disabled={disabled}
        aria-haspopup="menu"
        aria-expanded={open}
        className={cn(
          'inline-flex items-center justify-center gap-1.5 rounded-full px-3.5 py-1.5 text-xs font-bold transition active:scale-95 disabled:cursor-not-allowed disabled:opacity-50',
          TRIGGER_CLASS[variant],
        )}
      >
        <Download className="size-3.5" aria-hidden />
        Export
        <ChevronDown className={cn('size-3.5 transition-transform', open && 'rotate-180')} aria-hidden />
      </button>

      {open ? (
        <div
          role="menu"
          className={cn(
            'absolute right-0 z-40 w-48 overflow-hidden rounded-xl border border-border bg-surface p-1 shadow-pop',
            // The bulk bar is pinned to the bottom of the viewport, so its menu
            // must open upward; the toolbar menu opens downward as usual.
            variant === 'bulk' ? 'bottom-full mb-1.5' : 'mt-1.5',
          )}
        >
          {EXPORT_OPTIONS.map(({ format, label, Icon }) => (
            <button
              key={format}
              type="button"
              role="menuitem"
              onClick={() => { void runExport(format); }}
              disabled={busy !== null}
              className="flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-xs font-semibold text-text transition hover:bg-bg disabled:cursor-not-allowed disabled:opacity-50"
            >
              {busy === format ? (
                <Loader2 className="size-3.5 animate-spin text-text-faint" aria-hidden />
              ) : (
                <Icon className="size-3.5 text-text-faint" aria-hidden />
              )}
              {label}
            </button>
          ))}
          {error ? (
            <p role="alert" className="px-2.5 py-1.5 text-[11px] font-medium text-danger">
              {error}
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
