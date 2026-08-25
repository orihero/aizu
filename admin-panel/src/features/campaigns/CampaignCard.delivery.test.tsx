import { describe, expect, test } from 'vitest';
import { screen } from '@testing-library/react';
import { FakePanelRepository } from '@/test/fakePanelRepository';
import { buildCampaign, buildPanelState, buildRunBlock } from '@/test/fixtures';
import { renderWithProviders } from '@/test/renderWithProviders';
import type { Campaign } from '@/shared/types/domain';
import { CampaignCard } from './CampaignCard';

function renderCard(overrides: Partial<Campaign>) {
  const repository = new FakePanelRepository(buildPanelState());
  renderWithProviders(
    <CampaignCard campaign={buildCampaign(overrides)} run={buildRunBlock()} />,
    { repository },
  );
}

describe('CampaignCard found-vs-delivered (E.7)', () => {
  test('a not-delivered campaign shows BOTH numbers, never one alone', () => {
    // The measured prod shape: 15 harvested on the worker, 0 reached the cloud because
    // the job dead-lettered and leads only ship in the ack body.
    renderCard({
      leads: 0, leadsFound: 15, leadsDelivered: 0, delivery: 'not_delivered', spent: 4.2,
    });

    // Delivered is the headline count — those are the leads an operator can open…
    expect(screen.getByText('Leads').nextElementSibling).toHaveTextContent('0');
    // …and the found count sits beside it so the card doesn't deny work that happened.
    expect(screen.getByText('15 found · not delivered')).toBeInTheDocument();
  });

  test('the spend is labelled, never hidden or zeroed', () => {
    renderCard({ leads: 0, leadsFound: 15, leadsDelivered: 0, delivery: 'not_delivered', spent: 4.2 });

    expect(screen.getByText('$4.20')).toBeInTheDocument();
    expect(screen.getByText(/didn’t deliver its leads/i)).toBeInTheDocument();
  });

  test('CPL is left alone — never synthesised from the found count', () => {
    // CPL is guarded on WON leads, so "—" is the default state of every untriaged
    // campaign, healthy or not. Pricing leads the customer cannot open would be fiction.
    renderCard({ leads: 0, leadsFound: 15, leadsDelivered: 0, delivery: 'not_delivered', cpl: null });

    expect(screen.getByText('CPL').nextElementSibling).toHaveTextContent('—');
  });

  test('a delivered campaign carries no not-delivered state', () => {
    renderCard({ leads: 12, leadsFound: 12, leadsDelivered: 12, delivery: 'delivered' });

    expect(screen.getByText('Leads').nextElementSibling).toHaveTextContent('12');
    expect(screen.queryByText(/not delivered/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/didn’t deliver/i)).not.toBeInTheDocument();
  });

  test('a live PENDING gap is not flagged — it is ack lag, not a fault', () => {
    renderCard({ leads: 0, leadsFound: 9, leadsDelivered: 0, delivery: 'pending' });

    expect(screen.queryByText(/not delivered/i)).not.toBeInTheDocument();
  });

  test('a pre-v27 payload falls back to the plain lead count it always showed', () => {
    renderCard({ leads: 12, leadsFound: null, leadsDelivered: null, delivery: 'delivered' });

    expect(screen.getByText('Leads').nextElementSibling).toHaveTextContent('12');
    expect(screen.queryByText(/not delivered/i)).not.toBeInTheDocument();
  });
});
