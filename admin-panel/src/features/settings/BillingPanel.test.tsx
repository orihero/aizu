import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { FakePanelRepository } from '@/test/fakePanelRepository';
import { buildBilling, buildPanelState } from '@/test/fixtures';
import { renderWithProviders } from '@/test/renderWithProviders';
import type { Billing } from '@/shared/types/domain';
import { BillingPanel } from './BillingPanel';

/**
 * BillingPanel covers the self-serve upgrade path: it renders the plan/usage,
 * starts checkout with the selected {tier, interval}, opens the portal, and treats
 * the sales-led Scale tier as a mailto (never a checkout). Both mutations redirect
 * the browser, so window.location.assign is stubbed and asserted.
 */

function render(billing: Billing) {
  const repository = new FakePanelRepository(buildPanelState());
  renderWithProviders(<BillingPanel billing={billing} />, { repository });
  return repository;
}

let assignSpy: ReturnType<typeof vi.fn>;

beforeEach(() => {
  assignSpy = vi.fn();
  // jsdom's location.assign is a non-configurable noop, so swap the whole location
  // for a minimal plain object (routing uses MemoryRouter, so nothing else reads it).
  Object.defineProperty(window, 'location', {
    configurable: true,
    value: { assign: assignSpy, href: 'http://localhost/', origin: 'http://localhost' },
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('BillingPanel', () => {
  test('renders the current plan, status and usage meter', () => {
    render(buildBilling({ tier: 'starter', status: 'active', leadCap: 250, leadsUsed: 40, usageRatio: 0.16 }));
    // Plan name appears in the summary header and as the "Current" tier card.
    expect(screen.getAllByText('Starter').length).toBeGreaterThan(0);
    expect(screen.getByText('active')).toBeInTheDocument();
    expect(screen.getByText('40 / 250')).toBeInTheDocument();
  });

  test('Upgrade starts checkout with the selected tier and monthly interval by default', async () => {
    const repository = render(buildBilling({ tier: 'free' }));
    await userEvent.click(screen.getByRole('button', { name: 'Upgrade to Starter' }));
    await waitFor(() => { expect(repository.checkoutCalls).toHaveLength(1); });
    expect(repository.checkoutCalls[0]).toEqual({ tier: 'starter', interval: 'month' });
    expect(assignSpy).toHaveBeenCalledWith(repository.checkoutUrl);
  });

  test('the annual toggle switches the interval passed to checkout', async () => {
    const repository = render(buildBilling({ tier: 'free' }));
    await userEvent.click(screen.getByRole('button', { name: 'Annual' }));
    await userEvent.click(screen.getByRole('button', { name: 'Upgrade to Pro' }));
    await waitFor(() => { expect(repository.checkoutCalls).toHaveLength(1); });
    expect(repository.checkoutCalls[0]).toEqual({ tier: 'pro', interval: 'year' });
  });

  test('Scale is sales-led: a mailto link, never a checkout', () => {
    const repository = render(buildBilling({ tier: 'free' }));
    const salesLink = screen.getByRole('link', { name: 'Talk to sales' });
    expect(salesLink).toHaveAttribute('href', expect.stringContaining('mailto:'));
    // No "Upgrade to Scale" button exists.
    expect(screen.queryByRole('button', { name: /Upgrade to Scale/ })).not.toBeInTheDocument();
    expect(repository.checkoutCalls).toHaveLength(0);
  });

  test('the current tier shows "Your plan" and no Upgrade button for itself', () => {
    render(buildBilling({ tier: 'starter' }));
    expect(screen.getByText('Your plan')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Upgrade to Starter' })).not.toBeInTheDocument();
  });

  test('Manage billing opens the portal and redirects when the org has an account', async () => {
    const repository = render(buildBilling({ tier: 'starter' }));
    repository.hasBillingAccount = true;
    await userEvent.click(screen.getByRole('button', { name: /Manage billing/ }));
    await waitFor(() => { expect(repository.portalCalls).toBe(1); });
    expect(assignSpy).toHaveBeenCalledWith(repository.portalUrl);
  });

  test('a Free org with no Polar account shows a message and does not redirect', async () => {
    const repository = render(buildBilling({ tier: 'free' }));
    repository.hasBillingAccount = false;
    await userEvent.click(screen.getByRole('button', { name: /Manage billing/ }));
    await waitFor(() => { expect(repository.portalCalls).toBe(1); });
    expect(assignSpy).not.toHaveBeenCalled();
    expect(await screen.findByText(/No billing account yet/)).toBeInTheDocument();
  });

  test('an active paid subscriber changes plans via the portal, not a new checkout', async () => {
    // REGRESSION: a checkout only CREATES a subscription, which Polar rejects for
    // an existing subscriber ("You already have an active subscription"). On a paid
    // active plan, other tiers must offer "Change plan" → portal, never checkout.
    const repository = render(buildBilling({ tier: 'lite', status: 'active' }));
    repository.hasBillingAccount = true;
    // No "Upgrade to …" buttons exist while subscribed.
    expect(screen.queryByRole('button', { name: /^Upgrade to/ })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Change plan to Pro' }));
    await waitFor(() => { expect(repository.portalCalls).toBe(1); });
    expect(repository.checkoutCalls).toHaveLength(0);
    expect(assignSpy).toHaveBeenCalledWith(repository.portalUrl);
  });

  test('shows the celebration modal + plan limits when returning with ?checkout=success', async () => {
    const repository = new FakePanelRepository(buildPanelState());
    renderWithProviders(
      <BillingPanel billing={buildBilling({ tier: 'lite', status: 'active', leadCap: 50, leadsUsed: 5 })} />,
      { repository, route: '/settings/billing?checkout=success', path: '/settings/billing' },
    );
    // Celebration heading names the plan; the limits are shown.
    expect(await screen.findByText(/on Lite!/)).toBeInTheDocument();
    expect(screen.getByText(/Up to/)).toBeInTheDocument();
    expect(screen.getByText(/leads per billing period/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Go to dashboard' })).toBeInTheDocument();
  });

  test('does not show the celebration modal without the ?checkout=success marker', () => {
    render(buildBilling({ tier: 'lite', status: 'active' }));
    expect(screen.queryByText(/on Lite!/)).not.toBeInTheDocument();
  });

  test('at cap: the usage meter warns that new runs are blocked', () => {
    render(buildBilling({ tier: 'lite', leadCap: 50, leadsUsed: 50, usageRatio: 1 }));
    expect(screen.getByText(/Plan limit reached/)).toBeInTheDocument();
  });
});
