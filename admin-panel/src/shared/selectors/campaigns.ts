import type { Campaign, RunBlock, RunRecord } from '@/shared/types/domain';

/** A campaign the engine can actually run: not finished and has a matching brief. */
const NON_RUNNABLE_STATUS = 'ended';

export const CAMPAIGN_STATUS_LABEL: Readonly<Record<string, string>> = {
  live: 'Live',
  paused: 'Paused',
  draft: 'Draft',
  ended: 'Ended',
  archived: 'Archived',
};

export type CampaignStatusFilter = 'all' | 'live' | 'paused' | 'draft' | 'ended' | 'archived';

/** v12: archived is a reversible hide dimension, NOT a status. A non-null
 * `archivedAt` means the campaign is parked out of the active views. */
export function isArchived(campaign: Campaign): boolean {
  return campaign.archivedAt !== null;
}

/** v12: whether the campaign has an armed recurring schedule. */
export function isScheduled(campaign: Campaign): boolean {
  return campaign.scheduleEnabled;
}

const _DOW_LABEL = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'] as const;

/** A human cadence summary for the schedule badge, e.g. "Daily 09:00" or
 * "Weekly Mon 09:00". Returns null when the campaign is not scheduled. */
export function scheduleSummary(campaign: Campaign): string | null {
  if (!isScheduled(campaign)) return null;
  const hh = String(campaign.scheduleHour ?? 0).padStart(2, '0');
  const mm = String(campaign.scheduleMinute ?? 0).padStart(2, '0');
  const time = `${hh}:${mm}`;
  if (campaign.scheduleKind === 'daily') return `Daily ${time}`;
  if (campaign.scheduleKind === 'weekdays') return `Weekdays ${time}`;
  if (campaign.scheduleKind === 'weekly') {
    const day = _DOW_LABEL[campaign.scheduleDow ?? 0] ?? '';
    return `Weekly ${day} ${time}`;
  }
  return time;
}

/** Budget consumed as a 0–100 percentage (0 when no cap is set). */
export function budgetPct(campaign: Campaign): number {
  if (campaign.budgetCap <= 0) return 0;
  return Math.min(100, Math.round((campaign.spent / campaign.budgetCap) * 100));
}

export function selectActiveCampaignCount(campaigns: readonly Campaign[]): number {
  return campaigns.filter((c) => c.status === 'live' && !isArchived(c)).length;
}

/**
 * The server-enforced runnability rule (panel and engine must agree, mirroring
 * store.RUNNABLE_SQL_PREDICATE / cli._live_campaigns): a campaign is runnable iff
 * it hasn't ended, is not archived, and has a matching brief.
 */
export function isRunnable(campaign: Campaign): boolean {
  return (
    campaign.status !== NON_RUNNABLE_STATUS && !isArchived(campaign) && campaign.briefForm !== null
  );
}

export function selectRunnableCampaigns(campaigns: readonly Campaign[]): readonly Campaign[] {
  return campaigns.filter(isRunnable);
}

/**
 * Whether the campaign's backing account is warm enough to run (warming PRD §7.4).
 * The server is authoritative (`warmth.meetsGate`); we re-derive score>=gateMin
 * only as a trust check that the verdict matches the score it travelled with.
 */
export function isWarmEnough(campaign: Campaign): boolean {
  const w = campaign.warmth;
  return w.meetsGate && w.score >= w.gateMin;
}

/**
 * The full run gate the Run button uses: a runnable brief AND a warm-enough
 * account AND no run already in flight. `isRunnable` (brief) is checked first so
 * a draft never shows as "cold" when the real blocker is a missing brief.
 */
export function selectCampaignIsRunnable(campaign: Campaign, run: RunBlock): boolean {
  return isRunnable(campaign) && isWarmEnough(campaign) && !selectIsAnyRunActive(run);
}

/** Fallback tier from a raw score, for pre-warmth payloads that lack `state`. */
export function warmthTier(score: number, gateMin = 40, gateFull = 70): Campaign['warmth']['state'] {
  if (score < gateMin) return 'warming';
  if (score < gateFull) return 'ready';
  return 'full';
}

/** True while any run (single-campaign or batch) holds the process-global lock. */
export function selectIsAnyRunActive(run: RunBlock): boolean {
  return run.active !== null;
}

/** v12: true while the active run is cooperatively paused (idling between reels). */
export function selectIsRunPaused(run: RunBlock): boolean {
  return run.active?.paused === true;
}

/** True only for the specific campaign whose single run is currently in flight. */
export function selectIsCampaignRunning(run: RunBlock, campaignId: string): boolean {
  const active = run.active;
  return active !== null && active.scope === 'campaign' && active.campaignId === campaignId;
}

/**
 * The most recent finished run for this specific campaign (newest by startedAt),
 * or null. Batch ('all') runs are intentionally excluded: their summary is an
 * aggregate and would misattribute to a single card. This lets a card surface
 * what a background run actually did — including a failure that 202-accepted
 * then crashed, which otherwise looks like "nothing happened".
 */
export function selectLastRunForCampaign(run: RunBlock, campaignId: string): RunRecord | null {
  let latest: RunRecord | null = null;
  for (const record of run.recent) {
    if (record.scope !== 'campaign' || record.campaignId !== campaignId) continue;
    // ISO-8601 UTC timestamps compare correctly as strings.
    if (latest === null || record.startedAt > latest.startedAt) latest = record;
  }
  return latest;
}

export function selectCampaignsByStatus(
  campaigns: readonly Campaign[],
  status: CampaignStatusFilter,
): readonly Campaign[] {
  // v12: archived campaigns are hidden from every status view and surface ONLY
  // under the dedicated 'archived' filter.
  if (status === 'archived') return campaigns.filter(isArchived);
  const active = campaigns.filter((c) => !isArchived(c));
  return status === 'all' ? active : active.filter((c) => c.status === status);
}

export function selectStatusFilterCounts(
  campaigns: readonly Campaign[],
): Readonly<Record<CampaignStatusFilter, number>> {
  const counts: Record<CampaignStatusFilter, number> = {
    all: 0,
    live: 0,
    paused: 0,
    draft: 0,
    ended: 0,
    archived: 0,
  };
  for (const c of campaigns) {
    if (isArchived(c)) {
      counts.archived += 1;
      continue; // archived rows never count toward all/live/paused/draft/ended
    }
    counts.all += 1;
    if (c.status === 'live' || c.status === 'paused' || c.status === 'draft' || c.status === 'ended') {
      counts[c.status] += 1;
    }
  }
  return counts;
}
