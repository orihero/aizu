import type { Session } from '@/shared/types/domain';
import { selectByDay, selectTotals } from './sessions';

export interface RoutingSplit {
  readonly localText: number;
  readonly localVision: number;
  readonly cloud: number;
}

export interface SpendPoint {
  readonly date: string;
  readonly relevance: number;
  readonly scoring: number;
  readonly vision: number;
  readonly total: number;
}

const VISION_PASS_FACTOR = 1.6; // vision runs on relevant + unsure reels
const REL_WEIGHT_BASE = 0.42;
const REL_WEIGHT_STEP = 0.04;
const VISION_WEIGHT_BASE = 0.2;
const VISION_WEIGHT_STEP = 0.05;

const round2 = (value: number): number => Math.round(value * 100) / 100;

/** Local vs cloud call split — escalations are the cloud share. */
export function selectRoutingSplit(sessions: readonly Session[]): RoutingSplit {
  const totals = selectTotals(sessions);
  return {
    localText: totals.commentsScored + totals.reelsSeen - totals.escalations,
    localVision: Math.round(totals.relevant * VISION_PASS_FACTOR),
    cloud: totals.escalations,
  };
}

/**
 * Cumulative cloud spend by day, split per call site. Weights vary
 * deterministically per day; day sums equal session spend so the
 * series always reconciles with selectTotals().
 */
export function selectSpendSeries(sessions: readonly Session[]): readonly SpendPoint[] {
  interface Accumulator {
    readonly relevance: number;
    readonly scoring: number;
    readonly vision: number;
    readonly points: readonly SpendPoint[];
  }
  const result = selectByDay(sessions).reduce<Accumulator>(
    (acc, day, index) => {
      const relevanceWeight = REL_WEIGHT_BASE + (index % 3) * REL_WEIGHT_STEP;
      const visionWeight = VISION_WEIGHT_BASE + (index % 2) * VISION_WEIGHT_STEP;
      const dayRelevance = day.spend * relevanceWeight;
      const dayVision = day.spend * visionWeight;
      const dayScoring = day.spend - dayRelevance - dayVision;
      const relevance = acc.relevance + dayRelevance;
      const scoring = acc.scoring + dayScoring;
      const vision = acc.vision + dayVision;
      return {
        relevance,
        scoring,
        vision,
        points: [
          ...acc.points,
          {
            date: day.date,
            relevance: round2(relevance),
            scoring: round2(scoring),
            vision: round2(vision),
            total: round2(relevance + scoring + vision),
          },
        ],
      };
    },
    { relevance: 0, scoring: 0, vision: 0, points: [] },
  );
  return result.points;
}
