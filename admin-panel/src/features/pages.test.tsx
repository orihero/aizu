import { describe, expect, test } from 'vitest';
import { screen } from '@testing-library/react';
import { buildPanelState } from '@/test/fixtures';
import { FakePanelRepository } from '@/test/fakePanelRepository';
import { renderWithProviders } from '@/test/renderWithProviders';
import type { PanelState } from '@/shared/types/domain';
import { DashboardPage } from './dashboard/DashboardPage';
import { LeadsPage } from './leads/LeadsPage';
import { CampaignsPage } from './campaigns/CampaignsPage';
import { ReportsPage } from './reports/ReportsPage';
import { SettingsPage } from './settings/SettingsPage';

interface SetupOptions {
  readonly overrides?: Partial<PanelState>;
  readonly route?: string;
  readonly path?: string;
}

function setup(ui: React.ReactElement, { overrides = {}, route, path }: SetupOptions = {}) {
  const repository = new FakePanelRepository(buildPanelState(overrides));
  renderWithProviders(ui, {
    repository,
    ...(route ? { route } : {}),
    ...(path ? { path } : {}),
  });
  return { repository };
}

describe('DashboardPage', () => {
  test('renders the bento tiles from dashboard data', async () => {
    setup(<DashboardPage />);
    expect(await screen.findByText('Cost per lead')).toBeInTheDocument();
    expect(screen.getByText('Leads by channel')).toBeInTheDocument();
    expect(screen.getByText('Active campaigns')).toBeInTheDocument();
  });
});

describe('LeadsPage', () => {
  test('renders the stat row and a lead row', async () => {
    setup(<LeadsPage />, { route: '/leads', path: '/leads' });
    expect(await screen.findByText('Total leads')).toBeInTheDocument();
    expect(screen.getByText('Win rate')).toBeInTheDocument();
    expect(screen.getByText('dana_t', { exact: false })).toBeInTheDocument();
  });
});

describe('CampaignsPage', () => {
  test('renders campaign cards with the campaign name', async () => {
    setup(<CampaignsPage />);
    expect(await screen.findByText('Acme SaaS Lead Gen')).toBeInTheDocument();
  });
});

describe('ReportsPage', () => {
  test('renders the campaign performance table from report data', async () => {
    setup(<ReportsPage />);
    expect(await screen.findByText('Campaign performance')).toBeInTheDocument();
    expect(screen.getByText('Acme SaaS Lead Gen')).toBeInTheDocument();
  });
});

describe('SettingsPage', () => {
  test('renders the workspace panel by default', async () => {
    setup(<SettingsPage />, { route: '/settings', path: '/settings' });
    expect(await screen.findByDisplayValue('AIZU')).toBeInTheDocument();
    expect(screen.getByText('Team')).toBeInTheDocument();
  });

  test('renders the team panel on the team tab', async () => {
    setup(<SettingsPage />, { route: '/settings/team', path: '/settings/:tab' });
    expect(await screen.findByText('jane@acme.com')).toBeInTheDocument();
  });
});
