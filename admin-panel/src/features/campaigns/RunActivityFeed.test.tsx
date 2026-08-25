import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { buildFleetJob } from '@/test/fixtures';
import { EMPTY_RUN_ACTIVITY, type RunActivityState } from '@/shared/lib/runActivity';
import type { FleetJob } from '@/shared/types/domain';
import { FleetJobBanner, RunActivityFeed } from './RunActivityFeed';

const NOW_SEC = 1_718_800_000;

function activityWith(overrides: Partial<RunActivityState>): RunActivityState {
  return {
    ...EMPTY_RUN_ACTIVITY,
    runId: 'run-001',
    counters: {
      reelsSeen: 12, relevancePasses: 5, commentsScored: 40,
      matches: 3, spendUsd: 0.0123, likes: 2, follows: 1,
    },
    phase: 'searching',
    leadsFound: 3,
    leadsDelivered: 3,
    itemsScanned: 12,
    relevantFound: 5,
    lastEventAt: NOW_SEC,
    targetLeads: 10,
    ...overrides,
  };
}

describe('RunActivityFeed', () => {
  // Pin "now" so the liveness line ("last activity Ns ago") is deterministic.
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime((NOW_SEC + 8) * 1000);
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  test('leads found against the target is the primary progress', () => {
    render(<RunActivityFeed activity={activityWith({})} isError={false} />);

    expect(screen.getByText('3')).toBeInTheDocument();
    expect(screen.getByText('of 10 leads')).toBeInTheDocument();
    expect(screen.getByText('Searching for posts')).toBeInTheDocument();
  });

  test('a run with no known target still shows what it found', () => {
    render(<RunActivityFeed activity={activityWith({ targetLeads: null })} isError={false} />);

    expect(screen.getByText('leads found')).toBeInTheDocument();
  });

  test('falls back to the start response’s target for an in-process run', () => {
    // /api/run/activity only knows a FLEET job's target; an in-process run's reaches the
    // panel in the POST /api/run body. Without the hint the block has no denominator.
    render(
      <RunActivityFeed activity={activityWith({ targetLeads: null })} isError={false} targetLeadsHint={25} />,
    );

    expect(screen.getByText('of 25 leads')).toBeInTheDocument();
  });

  test('shows a liveness beat from lastEventAt', () => {
    render(<RunActivityFeed activity={activityWith({})} isError={false} />);

    expect(screen.getByText('Last activity 8s ago')).toBeInTheDocument();
  });

  test('a run that has emitted nothing says so rather than "0s ago"', () => {
    render(<RunActivityFeed activity={activityWith({ lastEventAt: null })} isError={false} />);

    expect(screen.getByText('No activity yet')).toBeInTheDocument();
  });

  test('renders NO narrative run events — the log left the customer app', () => {
    // B3. The component no longer accepts events at all, so the strongest assertion
    // available is that nothing list-shaped is rendered where the log used to be.
    render(<RunActivityFeed activity={activityWith({})} isError={false} />);

    expect(screen.queryByRole('list')).not.toBeInTheDocument();
    expect(screen.queryByRole('listitem')).not.toBeInTheDocument();
  });

  test('subordinate counters degrade to "—" instead of a fabricated zero', () => {
    render(
      <RunActivityFeed
        activity={activityWith({ itemsScanned: 0, relevantFound: 0, counters: null })}
        isError={false}
      />,
    );

    expect(screen.getByText('Posts seen')).toBeInTheDocument();
    expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(3);
    // The PRIMARY count is event-derived and therefore live, so a real 0 is honest there.
    expect(screen.getByText('3')).toBeInTheDocument();
  });

  test('a live FLEET run shows real progress while every session counter reads 0', () => {
    // E.1, the case this whole progress block exists for. A fleet job's session counters
    // (and its `matches` rows) travel in the ACK body, so mid-run the cloud aggregates
    // them all as 0 — while `run_events` land on the ~45s heartbeat. Driving the primary
    // number off the session counters is therefore a DEAD SCREEN for the entire run:
    // "0 of 10 leads, 0 posts seen" on a run that is working. `leadsFound` is
    // event-derived, so it is the one number that moves.
    render(
      <RunActivityFeed
        activity={activityWith({
          leadsFound: 4,
          targetLeads: 10,
          leadsDelivered: 0,
          delivery: 'pending',
          finished: false,
          itemsScanned: 0,
          relevantFound: 0,
          counters: {
            reelsSeen: 0, relevancePasses: 0, commentsScored: 0,
            matches: 0, spendUsd: 0, likes: 0, follows: 0,
          },
        })}
        isError={false}
      />,
    );

    expect(screen.getByText('4')).toBeInTheDocument();
    expect(screen.getByText('of 10 leads')).toBeInTheDocument();
    expect(screen.getByText('Last activity 8s ago')).toBeInTheDocument();
    // The zeroed session counters are shown as unknown, not as a contradicting "0 posts
    // seen" beside a run that has already found four leads.
    expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(2);
    // And an unacked live run is NOT accused of losing leads: 4 found / 0 delivered is
    // ack lag on every healthy fleet run, so `pending` must produce no warning.
    expect(screen.queryByText(/reached your account/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/incomplete run/i)).not.toBeInTheDocument();
  });

  test('a not-delivered run shows found AND delivered, and labels its spend', () => {
    // E.5/E.7: the measured prod case — 15 harvested, 0 delivered, spend banked.
    render(
      <RunActivityFeed
        activity={activityWith({
          finished: true, phase: 'failed', delivery: 'not_delivered',
          leadsFound: 15, leadsDelivered: 0,
        })}
        isError={false}
      />,
    );

    expect(screen.getByText(/found 15/i)).toBeInTheDocument();
    expect(screen.getByText(/0 reached your account/i)).toBeInTheDocument();
    expect(screen.getByText('Spend (incomplete run)')).toBeInTheDocument();
    // The spend itself is never hidden or zeroed — only labelled.
    expect(screen.getByText('$0.01')).toBeInTheDocument();
  });

  test('a delivered run carries no not-delivered warning', () => {
    render(<RunActivityFeed activity={activityWith({ finished: true, phase: 'done' })} isError={false} />);

    expect(screen.queryByText(/reached your account/i)).not.toBeInTheDocument();
    expect(screen.getByText('Spend')).toBeInTheDocument();
  });

  test('a live fleet run with a pending gap is NOT warned about', () => {
    // Every in-flight fleet run reads `pending` (rows land at ack); warning would stamp
    // an alarm on every healthy run.
    render(
      <RunActivityFeed
        activity={activityWith({ delivery: 'pending', leadsFound: 9, leadsDelivered: 0 })}
        isError={false}
      />,
    );

    expect(screen.queryByText(/reached your account/i)).not.toBeInTheDocument();
  });

  test('surfaces open flags', () => {
    const activity = activityWith({
      flags: [{ kind: 'feed_tapped_out', severity: 'warn', detail: 'no new reels' }],
    });

    render(<RunActivityFeed activity={activity} isError={false} />);

    expect(screen.getByText('feed_tapped_out')).toBeInTheDocument();
  });

  test('renders an error state without crashing', () => {
    render(<RunActivityFeed activity={null} isError />);

    expect(screen.getByText(/couldn’t load live progress/i)).toBeInTheDocument();
  });

  test('renders the fleet banner above the progress block for a fleet run', () => {
    render(
      <RunActivityFeed activity={activityWith({ fleetJob: buildFleetJob({ status: 'queued' }) })} isError={false} />,
    );
    expect(screen.getByText(/waiting for a worker/i)).toBeInTheDocument();
  });

  test('renders no fleet banner for an in-process run', () => {
    render(<RunActivityFeed activity={activityWith({ fleetJob: null })} isError={false} />);
    expect(screen.queryByText(/on the fleet/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/waiting for a worker/i)).not.toBeInTheDocument();
  });
});

describe('FleetJobBanner', () => {
  // Pin "now" so staleness (now - lastEventAt) is deterministic. lastEventAt = LAST_AT.
  const LAST_AT = 1_718_800_000;
  const NOW_MS = (LAST_AT + 15) * 1000; // 15s after last event → within threshold

  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW_MS);
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  function renderBanner(fleetJob: FleetJob | null) {
    return render(<FleetJobBanner fleetJob={fleetJob} />);
  }

  test('renders nothing for an in-process run (null)', () => {
    const { container } = renderBanner(null);
    expect(container).toBeEmptyDOMElement();
  });

  test('queued → waiting for a worker', () => {
    renderBanner(buildFleetJob({ status: 'queued', lastEventAt: null }));
    expect(screen.getByText(/waiting for a worker to pick this up/i)).toBeInTheDocument();
  });

  test('running with recent activity → live "last activity Xs ago"', () => {
    renderBanner(buildFleetJob({ status: 'running', lastEventAt: LAST_AT }));
    expect(screen.getByText(/running on fleet — last activity 15s ago/i)).toBeInTheDocument();
    expect(screen.queryByText(/stalled/i)).not.toBeInTheDocument();
  });

  test('leased is treated as live like running', () => {
    renderBanner(buildFleetJob({ status: 'leased', lastEventAt: LAST_AT }));
    expect(screen.getByText(/running on fleet/i)).toBeInTheDocument();
  });

  test('running with stale activity (older than threshold) → stalled warning', () => {
    // 200s ago exceeds the 120s stall threshold.
    renderBanner(buildFleetJob({ status: 'running', lastEventAt: LAST_AT - 200 }));
    expect(screen.getByText(/stalled — running on the fleet but no activity for 215s/i)).toBeInTheDocument();
  });

  test('running with no events yet (lastEventAt null) → stalled', () => {
    renderBanner(buildFleetJob({ status: 'running', lastEventAt: null }));
    expect(screen.getByText(/stalled — running on the fleet but no activity/i)).toBeInTheDocument();
  });

  test('done → finished on the fleet', () => {
    renderBanner(buildFleetJob({ status: 'done' }));
    expect(screen.getByText(/finished on the fleet/i)).toBeInTheDocument();
  });

  test('failed → finished on the fleet', () => {
    renderBanner(buildFleetJob({ status: 'failed' }));
    expect(screen.getByText(/finished on the fleet/i)).toBeInTheDocument();
  });

  test('failed with a known reason → says WHY, not just "finished" (B6)', () => {
    renderBanner(buildFleetJob({ status: 'failed', reason: 'cdp_unreachable' }));
    expect(
      screen.getByText(/failed on the fleet — the worker's chrome could not be attached/i),
    ).toBeInTheDocument();
  });

  test('failed with an unknown reason falls back to the raw code', () => {
    renderBanner(buildFleetJob({ status: 'failed', reason: 'some_new_code' }));
    expect(screen.getByText(/failed on the fleet — some_new_code/i)).toBeInTheDocument();
  });

  test('done with a reason is still "finished", never "failed"', () => {
    // ack overwrites `result` with the engine summary, whose halt_reason can be set on a
    // perfectly successful run (e.g. a daytime halt) — status is what decides the wording.
    renderBanner(buildFleetJob({ status: 'done', reason: 'daytime' }));
    expect(screen.getByText(/finished on the fleet/i)).toBeInTheDocument();
    expect(screen.queryByText(/failed/i)).not.toBeInTheDocument();
  });

  test('interrupted → finished on the fleet', () => {
    renderBanner(buildFleetJob({ status: 'interrupted' }));
    expect(screen.getByText(/finished on the fleet/i)).toBeInTheDocument();
  });
});
