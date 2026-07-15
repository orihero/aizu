import { describe, expect, test } from 'vitest';
import { buildMatch } from '@/test/fixtures';
import {
  selectEscalatedCount,
  selectLabeledCount,
  selectLanguageCounts,
  selectReviewQueue,
  selectStatusCounts,
} from './matches';

describe('selectStatusCounts', () => {
  test('counts every status bucket', () => {
    // Arrange
    const matches = [
      buildMatch({ id: 'a', status: 'new' }),
      buildMatch({ id: 'b', status: 'interested' }),
      buildMatch({ id: 'c', status: 'interested' }),
      buildMatch({ id: 'd', status: 'archived' }),
    ];

    // Act + Assert
    expect(selectStatusCounts(matches)).toEqual({
      new: 1,
      in_progress: 0,
      interested: 2,
      closed: 0,
      couldnt_connect: 0,
      archived: 1,
    });
  });
});

describe('selectReviewQueue', () => {
  test('returns only new matches sorted lowest-confidence first', () => {
    // Arrange
    const matches = [
      buildMatch({ id: 'high', score: 0.95, status: 'new' }),
      buildMatch({ id: 'done', score: 0.5, status: 'interested' }),
      buildMatch({ id: 'low', score: 0.62, status: 'new' }),
    ];

    // Act
    const queue = selectReviewQueue(matches);

    // Assert
    expect(queue.map((m) => m.id)).toEqual(['low', 'high']);
  });

  test('does not mutate the input order', () => {
    const matches = [
      buildMatch({ id: 'b', score: 0.9 }),
      buildMatch({ id: 'a', score: 0.6 }),
    ];
    selectReviewQueue(matches);
    expect(matches.map((m) => m.id)).toEqual(['b', 'a']);
  });
});

describe('selectLanguageCounts', () => {
  test('buckets null lang as unknown', () => {
    const matches = [
      buildMatch({ id: 'a', lang: 'uz' }),
      buildMatch({ id: 'b', lang: null }),
    ];
    expect(selectLanguageCounts(matches)).toEqual({ uz: 1, unknown: 1 });
  });
});

describe('selectLabeledCount / selectEscalatedCount', () => {
  test('labeled = terminal statuses (closed/couldnt_connect/archived); escalated counts flag', () => {
    const matches = [
      buildMatch({ id: 'a', status: 'closed', escalated: true }),
      buildMatch({ id: 'b', status: 'archived' }),
      buildMatch({ id: 'c', status: 'new' }),
    ];
    expect(selectLabeledCount(matches)).toBe(2);
    expect(selectEscalatedCount(matches)).toBe(1);
  });
});
