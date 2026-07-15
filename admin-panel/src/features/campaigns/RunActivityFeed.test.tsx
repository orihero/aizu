import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { buildFleetJob, buildRunEvent } from '@/test/fixtures';
import { EMPTY_RUN_ACTIVITY, type RunActivityState } from '@/shared/lib/runActivity';
import type { FleetJob } from '@/shared/types/domain';
import { FleetJobBanner, RunActivityFeed } from './RunActivityFeed';

function activityWith(overrides: Partial<RunActivityState>): RunActivityState {
  return {
    ...EMPTY_RUN_ACTIVITY,
    runId: 'run-001',
    counters: {
      reelsSeen: 12, relevancePasses: 5, commentsScored: 40,
      matches: 3, spendUsd: 0.0123, likes: 2, follows: 1,
    },
    ...overrides,
  };
}

describe('RunActivityFeed', () => {
  test('renders the live counters', () => {
    render(<RunActivityFeed activity={activityWith({})} isError={false} />);

    expect(screen.getByText('Reels')).toBeInTheDocument();
    expect(screen.getByText('12')).toBeInTheDocument();   // reelsSeen
    expect(screen.getByText('Leads')).toBeInTheDocument();
    expect(screen.getByText('3')).toBeInTheDocument();    // matches
  });

  test('lists event messages newest-first', () => {
    const activity = activityWith({
      events: [
        buildRunEvent({ id: 1, message: 'first event' }),
        buildRunEvent({ id: 2, message: 'second event' }),
      ],
    });

    render(<RunActivityFeed activity={activity} isError={false} />);

    const rows = screen.getAllByRole('listitem');
    expect(rows[0]).toHaveTextContent('second event'); // newest on top
    expect(rows[1]).toHaveTextContent('first event');
  });

  test('shows a waiting state before any event arrives on a live run', () => {
    render(<RunActivityFeed activity={activityWith({ events: [], finished: false })} isError={false} />);

    expect(screen.getByText(/waiting for the first event/i)).toBeInTheDocument();
  });

  test('a finished run with no events reads as recorded-none, not waiting', () => {
    render(<RunActivityFeed activity={activityWith({ events: [], finished: true })} isError={false} />);

    expect(screen.getByText(/no activity was recorded/i)).toBeInTheDocument();
    expect(screen.queryByText(/waiting for the first event/i)).not.toBeInTheDocument();
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

    expect(screen.getByText(/couldn’t load live activity/i)).toBeInTheDocument();
  });

  test('renders the fleet banner above the counters for a fleet run', () => {
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

  test('interrupted → finished on the fleet', () => {
    renderBanner(buildFleetJob({ status: 'interrupted' }));
    expect(screen.getByText(/finished on the fleet/i)).toBeInTheDocument();
  });
});
