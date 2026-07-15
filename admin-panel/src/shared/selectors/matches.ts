import type { Match, MatchStatus } from '@/shared/types/domain';

export type StatusCounts = Readonly<Record<MatchStatus, number>>;

const EMPTY_COUNTS: StatusCounts = {
  new: 0,
  in_progress: 0,
  interested: 0,
  closed: 0,
  couldnt_connect: 0,
  archived: 0,
};

export function selectStatusCounts(matches: readonly Match[]): StatusCounts {
  return matches.reduce<StatusCounts>(
    (acc, m) => ({ ...acc, [m.status]: acc[m.status] + 1 }),
    EMPTY_COUNTS,
  );
}

export function selectLanguageCounts(matches: readonly Match[]): Readonly<Record<string, number>> {
  return matches.reduce<Record<string, number>>((acc, m) => {
    const lang = m.lang ?? 'unknown';
    return { ...acc, [lang]: (acc[lang] ?? 0) + 1 };
  }, {});
}

export function selectPlatformCounts(matches: readonly Match[]): Readonly<Record<string, number>> {
  return matches.reduce<Record<string, number>>((acc, m) => {
    return { ...acc, [m.platform]: (acc[m.platform] ?? 0) + 1 };
  }, {});
}

/** New matches sorted lowest-confidence first (threshold-adjacent on top). */
export function selectReviewQueue(matches: readonly Match[]): readonly Match[] {
  return matches
    .filter((m) => m.status === 'new')
    .slice()
    .sort((a, b) => a.score - b.score);
}

/** Leads moved out of 'new' into a resolved/terminal state (operator has acted). */
export function selectLabeledCount(matches: readonly Match[]): number {
  const counts = selectStatusCounts(matches);
  return counts.closed + counts.couldnt_connect + counts.archived;
}

export function selectMatchesForReel(matches: readonly Match[], reelId: string): readonly Match[] {
  return matches.filter((m) => m.reelId === reelId);
}

export function selectEscalatedCount(matches: readonly Match[]): number {
  return matches.filter((m) => m.escalated).length;
}
