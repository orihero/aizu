import type { BadgeTone } from '@/shared/ui/Badge';
import type { LeadNote, Match, MatchStatus, StatusChange } from '@/shared/types/domain';

/**
 * The v6 lead Kanban pipeline. Engine snake_case keys → UI labels. This is the
 * single source of truth for status labels, column order, and tone across the
 * board, the pill, the toolbar filter, and the dashboard.
 */
export const LEAD_STATUS_LABEL: Readonly<Record<MatchStatus, string>> = {
  new: 'New',
  in_progress: 'In Progress',
  interested: 'Interested',
  closed: 'Closed',
  couldnt_connect: "Couldn't Connect",
  archived: 'Archived',
};

/** Pipeline order — drives the board columns, the status sort, and the filter list. */
export const LEAD_STATUS_ORDER: readonly MatchStatus[] = [
  'new',
  'in_progress',
  'interested',
  'closed',
  'couldnt_connect',
  'archived',
];

/** Badge tone per status (aliased to existing semantic tokens — no new CSS). */
export const LEAD_STATUS_TONE: Readonly<Record<MatchStatus, BadgeTone>> = {
  new: 'info',
  in_progress: 'warn',
  interested: 'cloud',
  closed: 'success',
  couldnt_connect: 'danger',
  archived: 'neutral',
};

/** Terminal/negative statuses — dropping a lead here requires a reason note. */
const TERMINAL_STATUSES: ReadonlySet<MatchStatus> = new Set([
  'closed',
  'couldnt_connect',
  'archived',
]);

export function isTerminalStatus(status: MatchStatus): boolean {
  return TERMINAL_STATUSES.has(status);
}

/** Statuses counted as a successful lead (mirrors the engine's WIN_STATUS). */
const WIN_STATUSES: ReadonlySet<MatchStatus> = new Set(['interested', 'closed']);

export interface LeadFilters {
  readonly query: string;
  readonly status: MatchStatus | 'all';
  /** A platform key, or the literal 'all' sentinel for no platform filter. */
  readonly platform: string;
  /** A campaign id, or the literal 'all' sentinel for no campaign filter. */
  readonly campaign: string;
}

export const EMPTY_LEAD_FILTERS: LeadFilters = {
  query: '', status: 'all', platform: 'all', campaign: 'all',
};

export interface LeadStats {
  readonly total: number;
  readonly newCount: number;
  /** Leads in a win status (interested + closed). */
  readonly won: number;
  readonly wonRate: number;
}

function matchesQuery(lead: Match, query: string): boolean {
  if (!query) return true;
  const q = query.toLowerCase();
  const extracted = Object.values(lead.extracted)
    .map((v) => (v == null ? '' : typeof v === 'string' ? v : JSON.stringify(v)))
    .join(' ');
  return (
    lead.username.toLowerCase().includes(q) ||
    lead.text.toLowerCase().includes(q) ||
    extracted.toLowerCase().includes(q)
  );
}

export function selectFilteredLeads(matches: readonly Match[], filters: LeadFilters): readonly Match[] {
  return matches.filter(
    (m) =>
      (filters.status === 'all' || m.status === filters.status) &&
      (filters.platform === 'all' || m.platform === filters.platform) &&
      matchesQuery(m, filters.query),
  );
}

/** Columns the operator can sort the Leads table by. */
export type LeadSortKey = 'username' | 'platform' | 'status' | 'score' | 'captured';

export interface LeadSort {
  readonly key: LeadSortKey;
  readonly dir: 'asc' | 'desc';
}

/** Default view: newest captures first (matches the engine's own ordering). */
export const DEFAULT_LEAD_SORT: LeadSort = { key: 'captured', dir: 'desc' };

/** Logical pipeline order so a status sort follows the Kanban column order. */
const STATUS_SORT_ORDER: Readonly<Record<MatchStatus, number>> = Object.fromEntries(
  LEAD_STATUS_ORDER.map((status, index) => [status, index]),
) as Record<MatchStatus, number>;

function compareByKey(a: Match, b: Match, key: LeadSortKey): number {
  switch (key) {
    case 'username':
      return a.username.localeCompare(b.username);
    case 'platform':
      return a.platform.localeCompare(b.platform);
    case 'status':
      return STATUS_SORT_ORDER[a.status] - STATUS_SORT_ORDER[b.status];
    case 'score':
      return a.score - b.score;
    case 'captured':
      return a.capturedAt.ts - b.capturedAt.ts;
  }
}

export function selectSortedLeads(leads: readonly Match[], sort: LeadSort): readonly Match[] {
  const direction = sort.dir === 'asc' ? 1 : -1;
  // Copy first — Array.sort mutates in place, and the input is read-only state.
  return [...leads].sort((a, b) => direction * compareByKey(a, b, sort.key));
}

export function selectLeadPage(
  leads: readonly Match[],
  page: number,
  perPage: number,
): readonly Match[] {
  const start = (page - 1) * perPage;
  return leads.slice(start, start + perPage);
}

export function pageCount(total: number, perPage: number): number {
  return Math.max(1, Math.ceil(total / perPage));
}

export function selectLeadStats(matches: readonly Match[]): LeadStats {
  const total = matches.length;
  const newCount = matches.filter((m) => m.status === 'new').length;
  const won = matches.filter((m) => WIN_STATUSES.has(m.status)).length;
  return {
    total,
    newCount,
    won,
    wonRate: total > 0 ? won / total : 0,
  };
}

/**
 * Resolve a lead by its composite `id`. Never match on `commentId`: the same comment
 * id legitimately exists under two campaigns (and on two platforms), so that lookup
 * returned whichever copy happened to sort first — the drawer then showed, and wrote
 * status to, the wrong campaign's lead.
 */
export function selectLeadById(matches: readonly Match[], leadId: string): Match | null {
  return matches.find((m) => m.id === leadId) ?? null;
}

/** A Kanban column: one status with the leads currently in it, in column order. */
export interface BoardColumn {
  readonly status: MatchStatus;
  readonly leads: readonly Match[];
}

/** Group leads into the six pipeline columns (empty columns preserved, in order). */
export function selectBoardColumns(leads: readonly Match[]): readonly BoardColumn[] {
  return LEAD_STATUS_ORDER.map((status) => ({
    status,
    leads: leads.filter((m) => m.status === status),
  }));
}

/** A merged activity-timeline item: an immutable status change or a deletable note. */
export type TimelineItem =
  | { readonly kind: 'status'; readonly ts: number; readonly change: StatusChange }
  | { readonly kind: 'note'; readonly ts: number; readonly note: LeadNote };

/** Status changes + notes, merged oldest-first, for the lead drawer's Activity feed. */
export function selectLeadTimeline(lead: Match): readonly TimelineItem[] {
  const changes: TimelineItem[] = lead.statusHistory.map((change) => ({
    kind: 'status',
    ts: change.atTs,
    change,
  }));
  const notes: TimelineItem[] = lead.notes.map((note) => ({
    kind: 'note',
    ts: note.createdAtTs,
    note,
  }));
  return [...changes, ...notes].sort((a, b) => a.ts - b.ts);
}

/**
 * A single export column: a header label and how to pull the cell value from a
 * lead. The single source of truth shared by every export format (CSV, Excel,
 * PDF) so the columns never drift between them. `numeric` cells render as typed
 * numbers in formats that distinguish types (Excel); the rest are plain text.
 */
export interface LeadExportColumn {
  readonly header: string;
  readonly value: (lead: Match) => string;
  readonly numeric?: boolean;
}

export const LEAD_EXPORT_COLUMNS: readonly LeadExportColumn[] = [
  { header: 'id', value: (l) => l.commentId },
  { header: 'username', value: (l) => l.username },
  { header: 'platform', value: (l) => l.platform },
  { header: 'status', value: (l) => LEAD_STATUS_LABEL[l.status] },
  { header: 'score', value: (l) => l.score.toFixed(2), numeric: true },
  { header: 'captured', value: (l) => `${l.capturedAt.date} ${l.capturedAt.time}` },
  { header: 'text', value: (l) => l.text },
];

/** Column headers, in order — the first row of every export. */
export const LEAD_EXPORT_HEADERS: readonly string[] = LEAD_EXPORT_COLUMNS.map((c) => c.header);

/** One lead as an ordered array of stringified cell values, aligned to the headers. */
export function leadToExportRow(lead: Match): readonly string[] {
  return LEAD_EXPORT_COLUMNS.map((c) => c.value(lead));
}

/** CSV text for client-side export of the currently-filtered leads. */
export function leadsToCsv(leads: readonly Match[]): string {
  const escape = (value: string) => `"${value.replace(/"/g, '""')}"`;
  const rows = leads.map((l) => leadToExportRow(l).map(escape).join(','));
  return [LEAD_EXPORT_HEADERS.join(','), ...rows].join('\n');
}
