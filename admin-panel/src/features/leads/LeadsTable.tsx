import { ChevronRight, ChevronsUpDown, ChevronDown, ChevronUp, Inbox } from 'lucide-react';
import { Avatar } from '@/shared/ui/Avatar';
import { EmptyState } from '@/shared/ui/EmptyState';
import { ScorePill } from '@/shared/ui/ScorePill';
import type { LeadSort, LeadSortKey } from '@/shared/selectors/leads';
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

/** Column headers. `key` is the sort field; a null key is a non-sortable column. */
const HEADERS: readonly { label: string; key: LeadSortKey | null }[] = [
  { label: 'Lead', key: 'username' },
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

  const pageIds = rows.map((row) => row.commentId);
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
          {rows.map((lead) => (
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
                  aria-label={`Select ${lead.username}`}
                  checked={selected.has(lead.commentId)}
                  onChange={() => { onToggleSelect(lead.commentId); }}
                  className="size-4 cursor-pointer accent-brand"
                />
              </td>
              <td className="max-w-72 px-3 py-3">
                <div className="flex items-center gap-2.5">
                  <Avatar username={lead.username} />
                  <div className="min-w-0">
                    <div className="font-semibold">{lead.username}</div>
                    <div className="truncate text-[10px] text-text-faint" title={lead.text}>
                      {lead.text}
                    </div>
                  </div>
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
          ))}
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
