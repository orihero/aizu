import { describe, expect, test } from 'vitest';
import { buildSession } from '@/test/fixtures';
import { selectByDay, selectLastSession, selectLiveSession, selectTotals } from './sessions';

describe('selectTotals', () => {
  test('sums every counter across sessions', () => {
    // Arrange
    const sessions = [
      buildSession({ reelsSeen: 10, matches: 2, spendUsd: 0.1, escalations: 1 }),
      buildSession({ id: 's-002', reelsSeen: 5, matches: 1, spendUsd: 0.25, escalations: 2 }),
    ];

    // Act
    const totals = selectTotals(sessions);

    // Assert
    expect(totals.reelsSeen).toBe(15);
    expect(totals.matches).toBe(3);
    expect(totals.escalations).toBe(3);
    expect(totals.spendUsd).toBeCloseTo(0.35);
  });

  test('returns zeros for no sessions', () => {
    expect(selectTotals([])).toEqual({
      reelsSeen: 0,
      alreadySeen: 0,
      relevant: 0,
      commentsScored: 0,
      matches: 0,
      escalations: 0,
      spendUsd: 0,
    });
  });
});

describe('selectByDay', () => {
  test('groups sessions per day preserving first-seen order', () => {
    // Arrange
    const sessions = [
      buildSession({ id: 's-1', date: 'Jun 9', reelsSeen: 10, spendUsd: 0.1 }),
      buildSession({ id: 's-2', date: 'Jun 10', reelsSeen: 20, spendUsd: 0.2 }),
      buildSession({ id: 's-3', date: 'Jun 9', reelsSeen: 5, spendUsd: 0.05 }),
    ];

    // Act
    const days = selectByDay(sessions);

    // Assert
    expect(days.map((d) => d.date)).toEqual(['Jun 9', 'Jun 10']);
    expect(days[0]?.reels).toBe(15);
    expect(days[0]?.sessions).toBe(2);
    expect(days[0]?.spend).toBeCloseTo(0.15);
    expect(days[1]?.reels).toBe(20);
  });
});

describe('selectLiveSession / selectLastSession', () => {
  test('finds the live session when one is running', () => {
    const live = buildSession({ id: 's-live', flag: 'live' });
    expect(selectLiveSession([buildSession(), live])).toBe(live);
  });

  test('returns null when nothing is live and last session otherwise', () => {
    const sessions = [buildSession({ id: 'a' }), buildSession({ id: 'b' })];
    expect(selectLiveSession(sessions)).toBeNull();
    expect(selectLastSession(sessions)?.id).toBe('b');
    expect(selectLastSession([])).toBeNull();
  });
});
