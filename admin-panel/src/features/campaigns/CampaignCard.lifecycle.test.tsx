import { describe, expect, test } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { buildCampaign, buildPanelState } from '@/test/fixtures';
import { FakePanelRepository } from '@/test/fakePanelRepository';
import { renderWithProviders } from '@/test/renderWithProviders';
import { CampaignCard } from './CampaignCard';
import { ScheduleDialog } from './ScheduleDialog';

const idleRun = { active: null, recent: [] };

function renderCard(campaign = buildCampaign({ status: 'live' })) {
  const repository = new FakePanelRepository(buildPanelState());
  renderWithProviders(<CampaignCard campaign={campaign} run={idleRun} />, { repository });
  return repository;
}

describe('CampaignCard — pause confirmation', () => {
  test('pausing a live campaign asks for confirmation before firing the mutation', async () => {
    const user = userEvent.setup();
    const repository = renderCard();

    await user.click(screen.getByRole('button', { name: 'Pause campaign' }));

    // Dialog is up; nothing has been written yet.
    expect(screen.getByText('Pause campaign?')).toBeInTheDocument();
    expect(repository.campaignCreates).toHaveLength(0);

    await user.click(screen.getByRole('button', { name: 'Pause' }));

    await waitFor(() => { expect(repository.campaignCreates).toHaveLength(1); });
    expect(repository.campaignCreates[0]).toMatchObject({ status: 'paused' });
  });

  test('cancelling the pause confirmation fires no mutation', async () => {
    const user = userEvent.setup();
    const repository = renderCard();

    await user.click(screen.getByRole('button', { name: 'Pause campaign' }));
    await user.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(screen.queryByText('Pause campaign?')).not.toBeInTheDocument();
    expect(repository.campaignCreates).toHaveLength(0);
  });

  test('resuming a paused campaign skips the confirmation (instant restore)', async () => {
    const user = userEvent.setup();
    const repository = renderCard(buildCampaign({ status: 'paused' }));

    await user.click(screen.getByRole('button', { name: 'Resume campaign' }));

    await waitFor(() => { expect(repository.campaignCreates).toHaveLength(1); });
    expect(repository.campaignCreates[0]).toMatchObject({ status: 'live' });
    expect(screen.queryByText('Pause campaign?')).not.toBeInTheDocument();
  });
});

describe('CampaignCard — archive confirmation', () => {
  test('archiving asks for confirmation before firing the mutation', async () => {
    const user = userEvent.setup();
    const repository = renderCard();

    // The Archive control is gated on `edit_campaigns`, which loads from the async
    // auth bootstrap — wait for it to mount.
    await user.click(await screen.findByRole('button', { name: 'Archive campaign' }));

    expect(screen.getByText('Archive campaign?')).toBeInTheDocument();
    expect(repository.archiveRequests).toHaveLength(0);

    await user.click(screen.getByRole('button', { name: 'Archive' }));

    await waitFor(() => { expect(repository.archiveRequests).toHaveLength(1); });
    expect(repository.archiveRequests[0]).toMatchObject({ archived: true });
  });

  test('un-archiving a parked campaign skips the confirmation (instant restore)', async () => {
    const user = userEvent.setup();
    const repository = renderCard(buildCampaign({ status: 'paused', archivedAt: 1_700_000_000 }));

    await user.click(await screen.findByRole('button', { name: 'Unarchive campaign' }));

    await waitFor(() => { expect(repository.archiveRequests).toHaveLength(1); });
    expect(repository.archiveRequests[0]).toMatchObject({ archived: false });
    expect(screen.queryByText('Archive campaign?')).not.toBeInTheDocument();
  });
});

describe('ScheduleDialog — run-style drawer', () => {
  test('renders as a side drawer (close button + subtitle) matching the run modal', () => {
    const repository = new FakePanelRepository(buildPanelState());
    renderWithProviders(
      <ScheduleDialog campaign={buildCampaign({ status: 'live' })} isOpen onClose={() => {}} />,
      { repository },
    );

    // Drawer chrome: the X close button and the subtitle line the run drawer uses.
    expect(screen.getByRole('button', { name: 'Close' })).toBeInTheDocument();
    expect(screen.getByText(/Recurring run/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Save schedule/ })).toBeInTheDocument();
  });
});
