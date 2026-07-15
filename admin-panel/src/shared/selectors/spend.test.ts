import { describe, expect, test } from 'vitest';
import { buildSession } from '@/test/fixtures';
import { selectTotals } from './sessions';
import { selectRoutingSplit, selectSpendSeries } from './spend';

describe('selectSpendSeries', () => {
  test('final cumulative total reconciles with session totals', () => {
    // Arrange
    const sessions = [
      buildSession({ id: 's-1', date: 'Jun 9', spendUsd: 0.2 }),
      buildSession({ id: 's-2', date: 'Jun 10', spendUsd: 0.3 }),
      buildSession({ id: 's-3', date: 'Jun 10', spendUsd: 0.1 }),
    ];

    // Act
    const series = selectSpendSeries(sessions);
    const last = series[series.length - 1];

    // Assert
    expect(series).toHaveLength(2);
    expect(last?.total).toBeCloseTo(selectTotals(sessions).spendUsd, 1);
  });

  test('series is cumulative (monotonically non-decreasing)', () => {
    const sessions = [
      buildSession({ id: 's-1', date: 'Jun 9', spendUsd: 0.2 }),
      buildSession({ id: 's-2', date: 'Jun 10', spendUsd: 0.3 }),
    ];
    const series = selectSpendSeries(sessions);
    expect(series[1]?.total ?? 0).toBeGreaterThanOrEqual(series[0]?.total ?? 0);
  });
});

describe('selectRoutingSplit', () => {
  test('cloud share equals escalations; local text excludes them', () => {
    const sessions = [
      buildSession({ commentsScored: 30, reelsSeen: 10, escalations: 4, relevant: 5 }),
    ];
    const split = selectRoutingSplit(sessions);
    expect(split.cloud).toBe(4);
    expect(split.localText).toBe(36);
    expect(split.localVision).toBe(8);
  });
});
