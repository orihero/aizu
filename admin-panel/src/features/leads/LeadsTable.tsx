import { ChevronRight, ChevronsUpDown, ChevronDown, ChevronUp, Inbox } from 'lucide-react';
import { cn } from '@/shared/lib/cn';
import { EmptyState } from '@/shared/ui/EmptyState';
import { ScorePill } from '@/shared/ui/ScorePill';
import { LEAD_INTENT_PLACEHOLDER, leadIntentLabel, type LeadSort, type LeadSortKey } from '@/shared/selectors/leads';
import type { Match } from '@/shared/types/domain';
import { LeadStatusPill } from './LeadStatusPill';
import { PlatformChip } from './PlatformChip';

interface LeadsTableProps {
  readonly rows: readonly Match[];
  readonly threshold: number;
  readonly selected: ReadonlySet<string>;
  readonly sort: LeadSort;
  readonly onSort: (key: LeadSortKey) => void;
  readonly onToggleSelect: (id: string) => void;
  /** Header checkbox — toggles selection of every row on the current page. */
  readonly onToggleSelectAll: (ids: readonly string[]) => void;
  readonly onOpen: (lead: Match) => void;
}

/**
 * Column headers. `key` is the sort field; a null key is a non-sortable column.
 *
 * v27: the identity column is an INTENT column. A row carries no handle, no comment
 * and no Avatar. The HANDLE is reachable one lead at a time through the drawer's
 * audited reveal; the COMMENT is not reachable at all — it is superadmin-only, and no
 * customer surface renders it.
 */
const HEADERS: readonly { label: string; key: LeadSortKey | null }[] = [
  { label: 'Intent', key: 'intent' },
  { label: 'Platform', key: 'platform' },
  { label: 'Status', key: 'status' },
  { label: 'Score', key: 'score' },
  { label: 'Captured', key: 'captured' },
  { label: '', key: null },
];

export function LeadsTable({
  rows,
  threshold,
  selected,
  sort,
  onSort,
  onToggleSelect,
  onToggleSelectAll,
  onOpen,
}: LeadsTableProps) {
  if (rows.length === 0) {
    return (
      <EmptyState
        icon={Inbox}
        title="No leads found"
        description="Try clearing the search or widening the status / platform filters."
      />
    );
  }

  // Composite lead ids — the one definition of identity, never the bare key.
  const pageIds = rows.map((row) => row.id);
  const allOnPageSelected = pageIds.every((id) => selected.has(id));

  return (
    <div className="overflow-x-auto px-2 py-1">
      <table className="w-full border-separate border-spacing-y-0.5 text-left text-xs">
        <thead>
          <tr className="text-[10.5px] font-bold uppercase tracking-wide text-text-faint">
            <th className="border-b border-border px-3 py-2.5">
              <input
                type="checkbox"
                aria-label="Select all leads on this page"
                checked={allOnPageSelected}
                onChange={() => { onToggleSelectAll(pageIds); }}
                className="size-4 cursor-pointer accent-brand"
              />
            </th>
            {/* HEADERS is a fixed tuple that never reorders — index keys are stable here. */}
            {HEADERS.map((header, index) => (
              <th key={index} className="border-b border-border px-3 py-2.5 font-bold">
                {header.key === null ? (
                  header.label
                ) : (
                  <button
                    type="button"
                    onClick={() => { onSort(header.key as LeadSortKey); }}
                    className="group/sort inline-flex items-center gap-1 font-bold uppercase tracking-wide text-text-faint transition-colors hover:text-text"
                    aria-label={`Sort by ${header.label}`}
                  >
                    {header.label}
                    <SortIcon active={sort.key === header.key} dir={sort.dir} />
                  </button>
                )}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((lead) => {
            const intent = leadIntentLabel(lead);
            return (
              <tr
                key={lead.id}
                onClick={() => { onOpen(lead); }}
                className="group cursor-pointer transition-colors hover:bg-surface-2"
              >
                <td
                  className="rounded-l-card px-3 py-3"
                  onClick={(event) => { event.stopPropagation(); }}
                >
                  <input
                    type="checkbox"
                    // No handle to name the row by any more; the intent is the row.
                    aria-label={`Select lead: ${intent}`}
                    checked={selected.has(lead.id)}
                    onChange={() => { onToggleSelect(lead.id); }}
                    className="size-4 cursor-pointer accent-brand"
                  />
                </td>
                <td className="max-w-96 px-3 py-3">
                  {/* Truncated to keep the row compact; the title carries the full intent. */}
                  <div
                    className={cn(
                      'truncate font-semibold',
                      intent === LEAD_INTENT_PLACEHOLDER && 'font-normal italic text-text-faint',
                    )}
                    title={intent}
                  >
                    {intent}
                  </div>
                </td>
                <td className="px-3 py-3">
                  <PlatformChip platform={lead.platform} />
                </td>
                <td className="px-3 py-3">
                  <LeadStatusPill status={lead.status} />
                </td>
                <td className="px-3 py-3">
                  <ScorePill score={lead.score} threshold={threshold} />
                </td>
                <td className="px-3 py-3 tabular-nums">
                  <div>{lead.capturedAt.date}</div>
                  <div className="text-[10px] text-text-faint">{lead.capturedAt.time}</div>
                </td>
                <td className="rounded-r-card px-3 py-3 text-text-faint transition-transform group-hover:translate-x-0.5 group-hover:text-text">
                  <ChevronRight className="size-3.5" aria-hidden />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/** Inactive columns show a faint dual-chevron; the active column shows its direction. */
function SortIcon({ active, dir }: { readonly active: boolean; readonly dir: LeadSort['dir'] }) {
  if (!active) {
    return (
      <ChevronsUpDown
        className="size-3 text-text-faint opacity-40 transition-opacity group-hover/sort:opacity-70"
        aria-hidden
      />
    );
  }
  const Icon = dir === 'asc' ? ChevronUp : ChevronDown;
  return <Icon className="size-3 text-text" aria-hidden />;
}
