import { describe, expect, test } from 'vitest';
import { screen } from '@testing-library/react';
import { FakePanelRepository } from '@/test/fakePanelRepository';
import { buildBilling, buildPanelState } from '@/test/fixtures';
import { renderWithProviders } from '@/test/renderWithProviders';
import type { Billing } from '@/shared/types/domain';
import type { Role } from '@/shared/auth/roles';
import { CampaignsPage } from './CampaignsPage';

function renderPage(billing: Billing | undefined, role: Role = 'owner') {
  const repository = new FakePanelRepository(buildPanelState());
  repository.billing = billing;
  repository.currentUser = repository.currentUser && { ...repository.currentUser, role };
  renderWithProviders(<CampaignsPage />, { repository, route: '/campaigns', path: '/campaigns' });
  return repository;
}

function planned(overrides: Partial<Billing> = {}): Billing {
  return buildBilling({ tier: 'free', campaignCap: 1, campaignsUsed: 1, ...overrides });
}

describe('CampaignsPage campaign allowance (v27 plan limits)', () => {
  test('shows how much of the campaign allowance is used', async () => {
    renderPage(planned({ campaignCap: 3, campaignsUsed: 1, tier: 'lite' }));

    expect(await screen.findByText('1 of 3 campaigns used')).toBeInTheDocument();
  });

  test('at the cap, New campaign is disabled with a reason and an upgrade link', async () => {
    renderPage(planned());

    // Wait for BILLING to land — until it does the action is (correctly) the plain link.
    expect(await screen.findByText(/Free includes 1 campaign/i)).toBeInTheDocument();
    // Not a link any more — the route still works, but offering it would send the
    // operator down a path we already know ends in a server 402.
    expect(screen.queryByRole('link', { name: /new campaign/i })).not.toBeInTheDocument();
    expect(screen.getByText('New campaign').closest('[aria-disabled="true"]')).not.toBeNull();
    expect(screen.getByRole('link', { name: /upgrade your plan/i }))
      .toHaveAttribute('href', '/settings/billing');
  });

  test('below the cap, New campaign stays a working link with no warning', async () => {
    renderPage(planned({ campaignCap: 3, campaignsUsed: 1, tier: 'lite' }));

    expect(await screen.findByRole('link', { name: /new campaign/i }))
      .toHaveAttribute('href', '/campaigns/new');
    expect(screen.queryByText(/upgrade your plan/i)).not.toBeInTheDocument();
  });

  test('an UNLIMITED cap (null) is never read as zero', async () => {
    // The gate is `campaignCap !== null && used >= cap`. A falsy check would disable
    // New campaign for every paying org on Starter and above.
    renderPage(planned({ tier: 'pro', campaignCap: null, campaignsUsed: 12 }));

    expect(await screen.findByText('12 campaigns · unlimited')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /new campaign/i })).toBeInTheDocument();
  });

  test('unknown billing leaves the action working rather than locking the page', async () => {
    renderPage(undefined);

    expect(await screen.findByRole('link', { name: /new campaign/i })).toBeInTheDocument();
    expect(screen.queryByText(/campaigns used/i)).not.toBeInTheDocument();
  });

  test('a viewer sees no New campaign action at all', async () => {
    renderPage(planned({ campaignCap: 3, campaignsUsed: 1 }), 'viewer');

    // Gating the whole action also keeps the BILLING fetch off a viewer's page, where
    // /api/settings would 403.
    await screen.findByText('Campaigns');
    expect(screen.queryByText('New campaign')).not.toBeInTheDocument();
    expect(screen.queryByText(/campaigns used/i)).not.toBeInTheDocument();
  });
});
