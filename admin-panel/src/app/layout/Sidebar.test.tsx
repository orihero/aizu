import { screen } from '@testing-library/react';
import { describe, expect, test } from 'vitest';
import { FakePanelRepository } from '@/test/fakePanelRepository';
import { buildCampaign, buildPanelState } from '@/test/fixtures';
import { renderWithProviders } from '@/test/renderWithProviders';
import { Sidebar } from './Sidebar';

function renderSidebar() {
  const state = buildPanelState({
    CAMPAIGNS: [
      buildCampaign({ id: 'cmp-001', name: 'Acme SaaS Lead Gen' }),
      buildCampaign({ id: 'cmp-002', name: 'Cross-Platform Apps' }),
    ],
  });
  const repository = new FakePanelRepository(state);
  renderWithProviders(<Sidebar />, { repository, route: '/leads', path: '/leads' });
  return repository;
}

describe('Sidebar', () => {
  test('renders the primary navigation links', async () => {
    renderSidebar();

    expect(await screen.findByRole('link', { name: /Dashboard/ })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /Campaigns/ })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /Leads/ })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /Reports/ })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /Settings/ })).toBeInTheDocument();
  });

  test('exposes the theme toggle and logout controls', async () => {
    renderSidebar();

    expect(await screen.findByLabelText('Toggle theme')).toBeInTheDocument();
    expect(screen.getByLabelText('Log out')).toBeInTheDocument();
  });

  test('does not render the campaign switcher or run-status indicator', async () => {
    renderSidebar();

    await screen.findByLabelText('Toggle theme');
    expect(screen.queryByLabelText('Switch active campaign')).not.toBeInTheDocument();
    expect(screen.queryByText(/Running ·/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Idle/)).not.toBeInTheDocument();
  });
});
