import type { Session } from '@/shared/types/domain';

export interface SessionTotals {
  readonly reelsSeen: number;
  readonly alreadySeen: number;
  readonly relevant: number;
  readonly commentsScored: number;
  readonly matches: number;
  readonly escalations: number;
  readonly spendUsd: number;
}

export interface DayAggregate {
  readonly date: string;
  readonly reels: number;
  readonly alreadySeen: number;
  readonly relevant: number;
  readonly matches: number;
  readonly spend: number;
  readonly sessions: number;
}

const round2 = (value: number): number => Math.round(value * 100) / 100;

export function selectTotals(sessions: readonly Session[]): SessionTotals {
  return sessions.reduce<SessionTotals>(
    (acc, s) => ({
      reelsSeen: acc.reelsSeen + s.reelsSeen,
      alreadySeen: acc.alreadySeen + s.alreadySeen,
      relevant: acc.relevant + s.relevant,
      commentsScored: acc.commentsScored + s.commentsScored,
      matches: acc.matches + s.matches,
      escalations: acc.escalations + s.escalations,
      spendUsd: round2(acc.spendUsd + s.spendUsd),
    }),
    {
      reelsSeen: 0,
      alreadySeen: 0,
      relevant: 0,
      commentsScored: 0,
      matches: 0,
      escalations: 0,
      spendUsd: 0,
    },
  );
}

/** Sessions grouped per calendar day, preserving first-seen order. */
export function selectByDay(sessions: readonly Session[]): readonly DayAggregate[] {
  const order: string[] = [];
  const byDate = new Map<string, DayAggregate>();
  for (const s of sessions) {
    const existing = byDate.get(s.date);
    if (!existing) order.push(s.date);
    const base: DayAggregate = existing ?? {
      date: s.date,
      reels: 0,
      alreadySeen: 0,
      relevant: 0,
      matches: 0,
      spend: 0,
      sessions: 0,
    };
    byDate.set(s.date, {
      date: base.date,
      reels: base.reels + s.reelsSeen,
      alreadySeen: base.alreadySeen + s.alreadySeen,
      relevant: base.relevant + s.relevant,
      matches: base.matches + s.matches,
      spend: round2(base.spend + s.spendUsd),
      sessions: base.sessions + 1,
    });
  }
  return order.map((date) => {
    const aggregate = byDate.get(date);
    if (!aggregate) throw new Error(`day aggregate missing for ${date}`);
    return aggregate;
  });
}

export function selectLiveSession(sessions: readonly Session[]): Session | null {
  return sessions.find((s) => s.flag === 'live') ?? null;
}

export function selectLastSession(sessions: readonly Session[]): Session | null {
  return sessions.length > 0 ? (sessions[sessions.length - 1] ?? null) : null;
}
