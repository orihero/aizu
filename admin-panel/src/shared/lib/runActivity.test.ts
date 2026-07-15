import { describe, expect, test } from 'vitest';
import { buildFleetJob, buildRunActivity, buildRunEvent } from '@/test/fixtures';
import { EMPTY_RUN_ACTIVITY, mergeRunActivity } from './runActivity';

describe('mergeRunActivity', () => {
  test('folds the first page into the empty accumulator', () => {
    const page = buildRunActivity({
      events: [buildRunEvent({ id: 1 }), buildRunEvent({ id: 2 })],
      cursor: 2,
    });

    const next = mergeRunActivity(EMPTY_RUN_ACTIVITY, page);

    expect(next.events.map((e) => e.id)).toEqual([1, 2]);
    expect(next.cursor).toBe(2);
    expect(next.runId).toBe('run-001');
  });

  test('appends only events newer than the cursor across pages', () => {
    const first = mergeRunActivity(
      EMPTY_RUN_ACTIVITY,
      buildRunActivity({ events: [buildRunEvent({ id: 1 }), buildRunEvent({ id: 2 })], cursor: 2 }),
    );

    const second = mergeRunActivity(
      first,
      buildRunActivity({ events: [buildRunEvent({ id: 3 }), buildRunEvent({ id: 4 })], cursor: 4 }),
    );

    expect(second.events.map((e) => e.id)).toEqual([1, 2, 3, 4]);
    expect(second.cursor).toBe(4);
  });

  test('drops a replayed/overlapping page so rows never double-list', () => {
    const first = mergeRunActivity(
      EMPTY_RUN_ACTIVITY,
      buildRunActivity({ events: [buildRunEvent({ id: 1 }), buildRunEvent({ id: 2 })], cursor: 2 }),
    );

    // Server re-sends ids 2 and 3 (overlap on 2).
    const second = mergeRunActivity(
      first,
      buildRunActivity({ events: [buildRunEvent({ id: 2 }), buildRunEvent({ id: 3 })], cursor: 3 }),
    );

    expect(second.events.map((e) => e.id)).toEqual([1, 2, 3]);
  });

  test('an empty page keeps the events and advances nothing past the cursor', () => {
    const first = mergeRunActivity(
      EMPTY_RUN_ACTIVITY,
      buildRunActivity({ events: [buildRunEvent({ id: 1 })], cursor: 1 }),
    );

    const second = mergeRunActivity(first, buildRunActivity({ events: [], cursor: 1 }));

    expect(second.events.map((e) => e.id)).toEqual([1]);
    expect(second.cursor).toBe(1);
  });

  test('reflects the latest snapshot of counters, flags and finished', () => {
    const next = mergeRunActivity(
      EMPTY_RUN_ACTIVITY,
      buildRunActivity({
        counters: {
          reelsSeen: 9, relevancePasses: 4, commentsScored: 30,
          matches: 2, spendUsd: 0.01, likes: 1, follows: 0,
        },
        flags: [{ kind: 'feed_tapped_out', severity: 'warn', detail: null }],
        finished: true,
        cursor: 1,
      }),
    );

    expect(next.counters?.reelsSeen).toBe(9);
    expect(next.flags).toHaveLength(1);
    expect(next.finished).toBe(true);
  });

  test('defaults fleetJob to null on the empty accumulator', () => {
    expect(EMPTY_RUN_ACTIVITY.fleetJob).toBeNull();
  });

  test('snapshot-replaces fleetJob from each page (not accumulated)', () => {
    const first = mergeRunActivity(
      EMPTY_RUN_ACTIVITY,
      buildRunActivity({ fleetJob: buildFleetJob({ status: 'queued' }), cursor: 1 }),
    );
    expect(first.fleetJob?.status).toBe('queued');

    // A later page reports the job now running — the newest snapshot wins.
    const second = mergeRunActivity(
      first,
      buildRunActivity({ fleetJob: buildFleetJob({ status: 'running' }), cursor: 1 }),
    );
    expect(second.fleetJob?.status).toBe('running');

    // An in-process page (fleetJob null) clears it rather than keeping the stale one.
    const third = mergeRunActivity(second, buildRunActivity({ fleetJob: null, cursor: 1 }));
    expect(third.fleetJob).toBeNull();
  });

  test('resets the accumulator when a different run is polled', () => {
    const first = mergeRunActivity(
      EMPTY_RUN_ACTIVITY,
      buildRunActivity({ runId: 'run-A', events: [buildRunEvent({ id: 7 })], cursor: 7 }),
    );

    const second = mergeRunActivity(
      first,
      buildRunActivity({ runId: 'run-B', events: [buildRunEvent({ id: 1 })], cursor: 1 }),
    );

    expect(second.runId).toBe('run-B');
    expect(second.events.map((e) => e.id)).toEqual([1]);
    expect(second.cursor).toBe(1);
  });
});
