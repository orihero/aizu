import type {
  ChannelDatum,
  Dashboard,
  DashboardPeriod,
  DashboardPeriodKey,
  Funnel,
  Match,
  MatchStatus,
} from '@/shared/types/domain';
import type { DonutDatum, FunnelStage, GroupedBarDatum } from '@/shared/ui/charts';
import { LEAD_STATUS_LABEL, LEAD_STATUS_ORDER } from '@/shared/selectors/leads';

/** The selectable period keys — shared by the Dashboard and Reports selectors. */
export const DASHBOARD_PERIOD_KEYS: readonly DashboardPeriodKey[] = ['today', 'week', 'month'];

/** Narrow an untrusted value (URL param / stored string) to a valid period key. */
export function asDashboardPeriod(raw: unknown): DashboardPeriodKey | null {
  return DASHBOARD_PERIOD_KEYS.includes(raw as DashboardPeriodKey)
    ? (raw as DashboardPeriodKey)
    : null;
}

const DAY_SECONDS = 86_400;
const WIN_STATUSES: ReadonlySet<MatchStatus> = new Set(['interested', 'closed']);
const TERMINAL_STATUSES: ReadonlySet<MatchStatus> = new Set([
  'closed',
  'couldnt_connect',
  'archived',
]);

function statusCount(matches: readonly Match[], status: MatchStatus): number {
  return matches.filter((m) => m.status === status).length;
}

/** Most recent activity epoch on a lead (status change, note, else capture). */
function lastActivityTs(lead: Match): number {
  return Math.max(
    lead.capturedAt.ts,
    lead.statusHistory.at(-1)?.atTs ?? 0,
    lead.notes.at(-1)?.createdAtTs ?? 0,
  );
}

export function selectPeriod(dashboard: Dashboard, period: DashboardPeriodKey): DashboardPeriod {
  return dashboard[period];
}

/** Channel tuples → grouped-bar rows (current vs previous). */
export function selectChannelData(channels: readonly ChannelDatum[]): GroupedBarDatum[] {
  return channels.map((c) => ({ category: c.platform, current: c.current, previous: c.previous }));
}

/** Funnel totals → ordered funnel stages (reels → relevant → scored → leads). */
export function selectFunnelStages(funnel: Funnel): FunnelStage[] {
  return [
    { name: 'Reels seen', value: funnel.reels },
    { name: 'Relevant', value: funnel.relevant },
    { name: 'Scored', value: funnel.scored },
    { name: 'Leads', value: funnel.matches },
  ];
}

/* ---- v6 lead-pipeline statistics (client-derived from MATCHES) ---- */

/** Count of leads in each of the six pipeline statuses (donut + legend). */
export function selectStatusDistribution(matches: readonly Match[]): DonutDatum[] {
  return LEAD_STATUS_ORDER.map((status) => ({
    name: LEAD_STATUS_LABEL[status],
    value: statusCount(matches, status),
  }));
}

/** Pipeline progression as funnel stages (new → in progress → interested → closed). */
export function selectPipelineStages(matches: readonly Match[]): FunnelStage[] {
  return [
    { name: 'New', value: statusCount(matches, 'new') },
    { name: 'In Progress', value: statusCount(matches, 'in_progress') },
    { name: 'Interested', value: statusCount(matches, 'interested') },
    { name: 'Closed', value: statusCount(matches, 'closed') },
  ];
}

/** Win rate = won (interested + closed) / total. 0 when there are no leads. */
export function selectWinRate(matches: readonly Match[]): number {
  if (matches.length === 0) return 0;
  const won = matches.filter((m) => WIN_STATUSES.has(m.status)).length;
  return won / matches.length;
}

/** Status changes per actor (email), busiest first — one bar per teammate. */
export function selectTeamActivity(matches: readonly Match[]): GroupedBarDatum[] {
  const counts = new Map<string, number>();
  for (const lead of matches) {
    for (const change of lead.statusHistory) {
      const who = change.by ?? 'system';
      counts.set(who, (counts.get(who) ?? 0) + 1);
    }
  }
  return [...counts.entries()]
    .sort((a, b) => b[1] - a[1])
    .map(([category, current]) => ({ category, current, previous: 0 }));
}

export interface NeedsAttention {
  /** Open 'in_progress' leads with no activity for `stuckDays`. */
  readonly stuckInProgress: number;
  /** Leads currently marked couldn't-connect. */
  readonly couldntConnect: number;
  /** Non-terminal leads with no activity for `idleDays`. */
  readonly noActivity: number;
}

export interface NeedsAttentionOptions {
  readonly now?: number; // epoch seconds; defaults to wall-clock
  readonly stuckDays?: number;
  readonly idleDays?: number;
}

/** Operational counters for the dashboard "needs attention" tile. */
export function selectNeedsAttention(
  matches: readonly Match[],
  options: NeedsAttentionOptions = {},
): NeedsAttention {
  const now = options.now ?? Date.now() / 1000;
  const stuckFloor = now - (options.stuckDays ?? 7) * DAY_SECONDS;
  const idleFloor = now - (options.idleDays ?? 14) * DAY_SECONDS;
  let stuckInProgress = 0;
  let couldntConnect = 0;
  let noActivity = 0;
  for (const lead of matches) {
    if (lead.status === 'couldnt_connect') couldntConnect += 1;
    const activity = lastActivityTs(lead);
    if (lead.status === 'in_progress' && activity < stuckFloor) stuckInProgress += 1;
    if (!TERMINAL_STATUSES.has(lead.status) && activity < idleFloor) noActivity += 1;
  }
  return { stuckInProgress, couldntConnect, noActivity };
}
