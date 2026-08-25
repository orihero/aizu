import { describe, expect, test } from 'vitest';
import { buildFleetJob, buildRunActivity, buildRunEvent } from '@/test/fixtures';
import type { RunActivity } from '@/shared/types/domain';
import {
  EMPTY_RUN_ACTIVITY,
  describeDelivery,
  lastActivityLabel,
  runPhaseLabel,
  snapshotRunActivity,
  subordinateCount,
  targetProgressPct,
  type RunActivityState,
} from './runActivity';

/**
 * One poll as the v27 bridge sends it. The Section E scalars are spelled out here rather
 * than leaned on from the shared fixture: this suite is the panel's description of that
 * payload contract, so it should still fail loudly if the fixture drifts away from it.
 */
function page(overrides: Partial<RunActivity> = {}): RunActivity {
  return buildRunActivity({
    phase: 'searching',
    eventsRedacted: true,
    leadsFound: 3,
    leadsDelivered: 3,
    delivery: 'delivered',
    itemsScanned: 12,
    relevantFound: 5,
    lastEventAt: 1_718_800_000,
    targetLeads: 10,
    ...overrides,
  });
}

function state(overrides: Partial<RunActivityState> = {}): RunActivityState {
  return { ...EMPTY_RUN_ACTIVITY, runId: 'run-001', ...overrides };
}

describe('snapshotRunActivity', () => {
  test('projects the Section E progress scalars off one page', () => {
    const next = snapshotRunActivity(page());

    expect(next.runId).toBe('run-001');
    expect(next.phase).toBe('searching');
    expect(next.leadsFound).toBe(3);
    expect(next.leadsDelivered).toBe(3);
    expect(next.delivery).toBe('delivered');
    expect(next.itemsScanned).toBe(12);
    expect(next.relevantFound).toBe(5);
    expect(next.lastEventAt).toBe(1_718_800_000);
    expect(next.targetLeads).toBe(10);
  });

  test('does not carry run events across the seam at all', () => {
    // Belt and braces: `events` is always [] for an org caller, but a pre-v27 bridge (or
    // a future regression) could still send rows. Dropping the key here means no
    // customer-facing component CAN render one — the redaction doesn't rely on every
    // component remembering not to.
    const next = snapshotRunActivity(page({ events: [buildRunEvent({ id: 1 })] }));

    expect('events' in next).toBe(false);
    expect(JSON.stringify(next)).not.toContain('Run started');
  });

  test('each page replaces the last — nothing accumulates across a run switch', () => {
    const first = snapshotRunActivity(page({ runId: 'run-A', leadsFound: 7 }));
    const second = snapshotRunActivity(page({ runId: 'run-B', leadsFound: 1 }));

    expect(first.leadsFound).toBe(7);
    expect(second.runId).toBe('run-B');
    expect(second.leadsFound).toBe(1);
  });

  test('snapshot-replaces the fleet job (null for an in-process run)', () => {
    expect(snapshotRunActivity(page({ fleetJob: buildFleetJob({ status: 'queued' }) })).fleetJob?.status)
      .toBe('queued');
    expect(snapshotRunActivity(page({ fleetJob: null })).fleetJob).toBeNull();
  });

  test('the empty state reads as "starting", not as "found nothing"', () => {
    // E.3: zero events on a run we are actively polling means it is starting up.
    expect(EMPTY_RUN_ACTIVITY.phase).toBe('starting');
    expect(EMPTY_RUN_ACTIVITY.leadsDelivered).toBeNull();
  });
});

describe('runPhaseLabel', () => {
  test('maps every customer-safe phase to plain words', () => {
    expect(runPhaseLabel('starting')).toBe('Starting up');
    expect(runPhaseLabel('searching')).toBe('Searching for posts');
    expect(runPhaseLabel('qualifying')).toBe('Reading comments');
    expect(runPhaseLabel('stopped')).toBe('Stopped');
    expect(runPhaseLabel('done')).toBe('Finished');
    expect(runPhaseLabel('failed')).toBe('Failed');
    // The server degrades an unrecognised phase to `working` rather than leaking a raw
    // internal string; this is the word a customer sees when it does.
    expect(runPhaseLabel('working')).toBe('Working');
  });
});

describe('lastActivityLabel', () => {
  const AT = 1_718_800_000;

  test('null when the run has emitted nothing (not "0s ago")', () => {
    expect(lastActivityLabel(null, AT * 1000)).toBeNull();
  });

  test('seconds, then minutes, then hours', () => {
    expect(lastActivityLabel(AT, (AT + 12) * 1000)).toBe('12s ago');
    expect(lastActivityLabel(AT, (AT + 4 * 60 + 5) * 1000)).toBe('4m ago');
    expect(lastActivityLabel(AT, (AT + 2 * 3600) * 1000)).toBe('2h ago');
  });

  test('a clock that runs backwards never prints a negative age', () => {
    expect(lastActivityLabel(AT, (AT - 30) * 1000)).toBe('0s ago');
  });
});

describe('subordinateCount', () => {
  test('renders "—" for anything unreported, INCLUDING zero', () => {
    // These counters ship in the ack body, so a fleet run reads 0 for its whole life and
    // a dead-lettered one reads 0 forever. Printing that as a real zero would claim the
    // run scanned nothing — the "unknown read as zero" mistake.
    expect(subordinateCount(0)).toBe('—');
    expect(subordinateCount(null)).toBe('—');
    expect(subordinateCount(undefined)).toBe('—');
  });

  test('formats a real count', () => {
    expect(subordinateCount(1200)).toBe('1,200');
  });
});

describe('targetProgressPct', () => {
  test('null when the run has no known target (no bar to draw)', () => {
    expect(targetProgressPct(3, null)).toBeNull();
    expect(targetProgressPct(3, 0)).toBeNull();
  });

  test('a plain fraction of the target', () => {
    expect(targetProgressPct(3, 10)).toBe(30);
  });

  test('clamps the BAR at 100 for an overshooting run', () => {
    // E.6: the target is a target, not a ceiling — a run really can pass it. The bar
    // clamps; the numbers beside it stay honest.
    expect(targetProgressPct(15, 10)).toBe(100);
  });
});

describe('describeDelivery', () => {
  test('nothing to explain on a delivered run', () => {
    expect(describeDelivery(state({ delivery: 'delivered', leadsFound: 5, leadsDelivered: 5 })))
      .toBeNull();
  });

  test('a PENDING gap is ack lag, not a fault — no warning', () => {
    // Every live fleet run reads this way (rows land at ack). Warning here would stamp
    // an alarm on every healthy run.
    expect(describeDelivery(state({ delivery: 'pending', leadsFound: 9, leadsDelivered: 0 })))
      .toBeNull();
  });

  test('a not-delivered run names BOTH numbers', () => {
    const notice = describeDelivery(
      state({ delivery: 'not_delivered', leadsFound: 15, leadsDelivered: 0, finished: true }),
    );

    // Neither number alone: 0 denies work that happened, 15 implies leads that can be opened.
    expect(notice?.headline).toContain('15');
    expect(notice?.headline).toContain('0');
    expect(notice?.detail).toMatch(/spend on an incomplete run/i);
  });

  test('an unknown delivered count says so rather than printing a zero', () => {
    const notice = describeDelivery(
      state({ delivery: 'not_delivered', leadsFound: 15, leadsDelivered: null, finished: true }),
    );

    expect(notice?.headline).toMatch(/never confirmed/i);
    expect(notice?.headline).not.toMatch(/\b0\b/);
  });

  test('null activity produces no notice', () => {
    expect(describeDelivery(null)).toBeNull();
  });
});
