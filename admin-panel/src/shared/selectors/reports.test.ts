import { describe, expect, test } from 'vitest';
import { buildReportsPeriod } from '@/test/fixtures';
import {
  selectDonutData,
  selectLineSeries,
  selectPlatformRanking,
  selectReportPeriod,
  selectTotalSpend,
} from './reports';

describe('selectReportPeriod', () => {
  test('picks the requested period', () => {
    const reports = {
      today: buildReportsPeriod({ labels: ['t'] }),
      week: buildReportsPeriod({ labels: ['w'] }),
      month: buildReportsPeriod({ labels: ['m'] }),
    };
    expect(selectReportPeriod(reports, 'week').labels).toEqual(['w']);
  });
});

describe('selectLineSeries', () => {
  test('maps matchesByPlatform to named series', () => {
    const period = buildReportsPeriod({
      matchesByPlatform: [
        { platform: 'instagram', values: [1, 2, 3] },
        { platform: 'youtube', values: [0, 1, 0] },
      ],
    });
    expect(selectLineSeries(period)).toEqual([
      { name: 'instagram', values: [1, 2, 3] },
      { name: 'youtube', values: [0, 1, 0] },
    ]);
  });
});

describe('selectDonutData / selectTotalSpend', () => {
  test('passes through stages and sums spend', () => {
    const period = buildReportsPeriod({
      spendByStage: [
        { name: 'match', value: 0.05 },
        { name: 'vision', value: 0.09 },
      ],
    });
    expect(selectDonutData(period)).toHaveLength(2);
    expect(selectTotalSpend(period)).toBeCloseTo(0.14);
  });
});

describe('selectPlatformRanking', () => {
  test('computes a 0–1 share relative to the top platform', () => {
    const period = buildReportsPeriod({
      platformRanking: [
        { platform: 'instagram', leads: 12 },
        { platform: 'youtube', leads: 3 },
      ],
    });
    const ranked = selectPlatformRanking(period);
    expect(ranked[0]?.share).toBe(1);
    expect(ranked[1]?.share).toBeCloseTo(0.25);
  });

  test('handles an empty ranking without dividing by zero', () => {
    const period = buildReportsPeriod({ platformRanking: [] });
    expect(selectPlatformRanking(period)).toEqual([]);
  });
});
