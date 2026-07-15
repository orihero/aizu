import { useEffect, useRef, useState } from 'react';
import { ChevronDown, X } from 'lucide-react';
import { cn } from '@/shared/lib/cn';
import { useBulkSetStatus } from '@/shared/hooks/useWriteMutations';
import { useCan } from '@/shared/hooks/useCan';
import { LEAD_STATUS_LABEL, LEAD_STATUS_ORDER, isTerminalStatus } from '@/shared/selectors/leads';
import type { Match, MatchStatus } from '@/shared/types/domain';
import { LeadsExportMenu } from './LeadsExportMenu';
import { BulkReasonModal } from './BulkReasonModal';

interface LeadsBulkBarProps {
  /** The fully-resolved selected leads (already mapped from selected ids). */
  readonly selectedLeads: readonly Match[];
  readonly onClear: () => void;
}

// Dark-ink pills on the lime bar — accent-ink is the readable counterpart to
// the accent fill in both themes.
const PILL_BTN =
  'inline-flex items-center gap-1.5 rounded-full border border-accent-ink/15 bg-accent-ink/10 px-3 py-1.5 text-xs font-bold text-accent-ink transition hover:bg-accent-ink/20 active:scale-95 disabled:cursor-not-allowed disabled:opacity-50';

type BulkItem = { readonly commentId: string; readonly platform?: string };

/**
 * Group the selection by campaignId. The engine matches a status write on
 * (campaignId, platform, commentId), so a selection spanning campaigns must be
 * split into one bulk write per campaign — a single shared campaignId would
 * silently miss every lead from the other campaigns.
 */
function groupByCampaign(leads: readonly Match[]): ReadonlyMap<string, readonly BulkItem[]> {
  return leads.reduce<Map<string, readonly BulkItem[]>>((acc, lead) => {
    const items = acc.get(lead.campaignId) ?? [];
    acc.set(lead.campaignId, [...items, { commentId: lead.commentId, platform: lead.platform }]);
    return acc;
  }, new Map());
}

export function LeadsBulkBar({ selectedLeads, onClear }: LeadsBulkBarProps) {
  const bulkSetStatus = useBulkSetStatus();
  // Bulk status changes (incl. archive) are owner/admin only. Viewers/members
  // can still select + export (read), but not bulk-change status.
  const canBulkEdit = useCan('bulk_edit_leads');
  const [menuOpen, setMenuOpen] = useState(false);
  // The terminal status awaiting a shared reason, or null when no modal is open.
  const [pendingTarget, setPendingTarget] = useState<MatchStatus | null>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const count = selectedLeads.length;

  useEffect(() => {
    if (!menuOpen) return;
    const onPointerDown = (event: MouseEvent) => {
      if (!menuRef.current?.contains(event.target as Node)) setMenuOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setMenuOpen(false);
    };
    document.addEventListener('mousedown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('mousedown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [menuOpen]);

  if (count === 0) return null;

  const applyStatus = async (status: MatchStatus, note?: string) => {
    if (selectedLeads.length === 0) return;
    try {
      for (const [campaignId, items] of groupByCampaign(selectedLeads)) {
        // exactOptionalPropertyTypes is on — omit `note` rather than send undefined.
        await bulkSetStatus.mutateAsync({
          campaignId,
          status,
          items: [...items],
          ...(note ? { note } : {}),
        });
      }
      onClear();
    } catch {
      // The mutation surfaces the failure via its own state; keep the selection
      // so the operator can retry rather than losing their picks.
    }
  };

  const onPickStatus = (status: MatchStatus) => {
    setMenuOpen(false);
    // Terminal moves (closed/couldnt_connect/archived) need a shared reason note.
    if (isTerminalStatus(status)) {
      setPendingTarget(status);
      return;
    }
    void applyStatus(status);
  };

  const confirmTerminal = (reason: string) => {
    const target = pendingTarget;
    setPendingTarget(null);
    if (target) void applyStatus(target, reason);
  };

  return (
    <>
      <div className="fixed inset-x-0 bottom-6 z-30 flex justify-center px-3">
        <div className="flex flex-wrap items-center gap-2.5 rounded-full bg-accent px-4 py-2.5 text-accent-ink shadow-pop">
          <span className="whitespace-nowrap px-1 text-xs font-bold tabular-nums">
            {count} selected
          </span>
          <span className="h-4 w-px bg-accent-ink/20" aria-hidden />
          {canBulkEdit ? (
            <div ref={menuRef} className="relative">
              <button
                type="button"
                onClick={() => { setMenuOpen((v) => !v); }}
                disabled={bulkSetStatus.isPending}
                aria-haspopup="menu"
                aria-expanded={menuOpen}
                className={PILL_BTN}
              >
                Set status
                <ChevronDown
                  className={cn('size-3.5 transition-transform', menuOpen && 'rotate-180')}
                  aria-hidden
                />
              </button>
              {menuOpen ? (
                <div
                  role="menu"
                  className="absolute bottom-full left-0 z-40 mb-1.5 w-52 overflow-hidden rounded-xl border border-border bg-surface p-1 shadow-pop"
                >
                  {LEAD_STATUS_ORDER.map((status) => (
                    <button
                      key={status}
                      type="button"
                      role="menuitem"
                      onClick={() => { onPickStatus(status); }}
                      className="flex w-full items-center justify-between gap-2.5 rounded-lg px-2.5 py-2 text-left text-xs font-semibold text-text transition hover:bg-bg"
                    >
                      {LEAD_STATUS_LABEL[status]}
                      {isTerminalStatus(status) ? (
                        <span className="text-[10px] font-medium text-text-faint">needs reason</span>
                      ) : null}
                    </button>
                  ))}
                </div>
              ) : null}
            </div>
          ) : null}
          <LeadsExportMenu leads={selectedLeads} variant="bulk" />
          <span className="h-4 w-px bg-accent-ink/20" aria-hidden />
          <button
            type="button"
            onClick={onClear}
            aria-label="Clear selection"
            className="flex size-7 items-center justify-center rounded-full bg-accent-ink/10 text-accent-ink/70 transition hover:bg-accent-ink/20 hover:text-accent-ink"
          >
            <X className="size-3.5" aria-hidden />
          </button>
        </div>
      </div>

      <BulkReasonModal
        target={pendingTarget}
        count={count}
        onCancel={() => { setPendingTarget(null); }}
        onConfirm={confirmTerminal}
        isSubmitting={bulkSetStatus.isPending}
      />
    </>
  );
}
