import { describe, expect, test } from 'vitest';
import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { buildPanelState, buildRunActivity, buildRunEvent, buildSession } from '@/test/fixtures';
import { FakePanelRepository } from '@/test/fakePanelRepository';
import { renderWithProviders } from '@/test/renderWithProviders';
import { CampaignRunHistory } from './CampaignRunHistory';

function renderHistory(sessions: ReturnType<typeof buildSession>[], repo?: FakePanelRepository) {
  const repository = repo ?? new FakePanelRepository(buildPanelState());
  renderWithProviders(<CampaignRunHistory sessions={sessions} />, { repository });
  return repository;
}

describe('CampaignRunHistory', () => {
  test('lists runs newest-first (server returns oldest-first)', () => {
    renderHistory([
      buildSession({ id: 's1', date: 'Jun 10', start: '09:00' }),
      buildSession({ id: 's2', date: 'Jun 11', start: '14:30' }),
    ]);

    const rows = screen.getAllByRole('listitem');
    expect(rows[0]).toHaveTextContent('Jun 11 · 14:30'); // newest on top
    expect(rows[1]).toHaveTextContent('Jun 10 · 09:00');
  });

  test('maps the session flag to a status badge', () => {
    renderHistory([
      buildSession({ id: 's1', flag: '' }),
      buildSession({ id: 's2', flag: 'halted' }),
      buildSession({ id: 's3', flag: 'live' }),
    ]);

    expect(screen.getByText('Completed')).toBeInTheDocument();
    expect(screen.getByText('Halted')).toBeInTheDocument();
    expect(screen.getByText('Running')).toBeInTheDocument();
  });

  test('shows each run’s key metrics', () => {
    renderHistory([buildSession({ matches: 7, reelsSeen: 42, spendUsd: 1.23, durationMin: 18 })]);

    const row = screen.getByRole('listitem');
    expect(row).toHaveTextContent('18 min');
    expect(within(row).getByText('7')).toBeInTheDocument(); // leads
    expect(row).toHaveTextContent('42 reels');
    expect(row).toHaveTextContent('$1.23');
  });

  test('renders an empty state when there are no runs', () => {
    renderHistory([]);

    expect(screen.getByText('No runs yet')).toBeInTheDocument();
    expect(screen.queryByRole('listitem')).not.toBeInTheDocument();
  });

  test('a run without a runId is not interactive', () => {
    renderHistory([buildSession({ runId: null })]);

    expect(screen.queryByRole('button')).not.toBeInTheDocument();
    expect(screen.getByText(/no details available/i)).toBeInTheDocument();
  });

  test('clicking a run opens its recorded outcome', async () => {
    const user = userEvent.setup();
    const repo = new FakePanelRepository(buildPanelState());
    repo.runActivity = buildRunActivity({
      runId: 'run-xyz',
      finished: true,
      phase: 'done',
      leadsFound: 4,
      leadsDelivered: 4,
      delivery: 'delivered',
      targetLeads: 10,
    });
    renderHistory([buildSession({ id: 's1', runId: 'run-xyz' })], repo);

    await user.click(screen.getByRole('button'));

    expect(await screen.findByText('of 10 leads')).toBeInTheDocument();
    expect(screen.getByText('Finished')).toBeInTheDocument();
    expect(repo.runActivityFetches[0]?.runId).toBe('run-xyz');
  });

  test('the opened run shows NO narrative event text (B3)', async () => {
    // The log is a superadmin surface now: a match event's detail carries the very
    // handle and comment the org-facing payload redacts, so a "filtered" feed would
    // have shipped exactly those rows and trusted a filter to drop them.
    const user = userEvent.setup();
    const repo = new FakePanelRepository(buildPanelState());
    repo.runActivity = buildRunActivity({
      runId: 'run-xyz',
      finished: true,
      events: [buildRunEvent({ id: 1, message: 'matched @dana_t (0.91)' })],
    });
    renderHistory([buildSession({ id: 's1', runId: 'run-xyz' })], repo);

    await user.click(screen.getByRole('button'));

    await screen.findByText('Posts seen'); // the drawer's progress block has painted
    expect(screen.queryByText(/dana_t/)).not.toBeInTheDocument();
  });
});
