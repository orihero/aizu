import { describe, expect, test, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { AppProviders } from '@/app/providers';
import { buildAlert, buildMatch, buildPanelState } from '@/test/fixtures';
import { FakePanelRepository } from '@/test/fakePanelRepository';
import type { PanelState } from '@/shared/types/domain';
import { AppLayout } from './AppLayout';

function renderWithRepo(repository: FakePanelRepository) {
  render(
    <AppProviders repository={repository}>
      <MemoryRouter initialEntries={['/overview']}>
        <Routes>
          <Route element={<AppLayout />}>
            <Route path="/overview" element={<div>overview body</div>} />
          </Route>
        </Routes>
      </MemoryRouter>
    </AppProviders>,
  );
}

function renderLayout(overrides: Partial<PanelState> = {}) {
  const repository = new FakePanelRepository(buildPanelState(overrides));
  render(
    <AppProviders repository={repository}>
      <MemoryRouter initialEntries={['/overview']}>
        <Routes>
          <Route element={<AppLayout />}>
            <Route path="/overview" element={<div>overview body</div>} />
            <Route path="/health" element={<div>health body</div>} />
          </Route>
        </Routes>
      </MemoryRouter>
    </AppProviders>,
  );
  return { repository };
}

describe('AppLayout chrome', () => {
  test('sidebar shows nav and the live match count from data', async () => {
    renderLayout({
      MATCHES: [
        buildMatch({ id: 'a', commentId: 'a', status: 'new' }),
        buildMatch({ id: 'b', commentId: 'b', status: 'new' }),
      ],
    });
    const leadsLink = await screen.findByRole('link', { name: /Leads/ });
    expect(leadsLink).toHaveTextContent('2');
  });

  test('halt banner appears only when health is halted and can be dismissed', async () => {
    const state = buildPanelState();
    const user = userEvent.setup();
    renderLayout({
      HEALTH: { ...state.HEALTH, overall: 'halted' },
      ALERTS: [buildAlert({ tier: 'halt', title: 'Checkpoint Challenge', desc: 'manual login required' })],
    });
    const banner = await screen.findByRole('alert');
    expect(banner).toHaveTextContent('Checkpoint Challenge');

    await user.click(screen.getByRole('button', { name: 'Dismiss' }));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  test('no halt banner when operational', async () => {
    renderLayout();
    await screen.findByText('overview body');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  test('org impersonation banner shows for an impersonated session and exits (Gap F)', async () => {
    const user = userEvent.setup();
    const assign = vi.fn();
    // Replace location on window (jsdom's location.assign itself is non-configurable);
    // build a plain object explicitly rather than spreading the Location instance. The
    // app uses MemoryRouter here, so only assign is exercised.
    const realLocation = window.location;
    Object.defineProperty(window, 'location', {
      value: { assign, href: realLocation.href, origin: realLocation.origin },
      writable: true, configurable: true,
    });
    const repository = new FakePanelRepository(buildPanelState());
    repository.currentUser = {
      id: null, email: 'ops@aizu.test', role: 'owner', orgId: 7,
      org: { id: 7, name: 'AcmeCorp', logo: null, description: null },
      impersonated: true,
    };
    renderWithRepo(repository);

    const banner = await screen.findByRole('alert');
    expect(banner).toHaveTextContent(/Viewing AcmeCorp as a platform admin/i);

    await user.click(screen.getByRole('button', { name: /exit impersonation/i }));
    await waitFor(() => { expect(repository.endImpersonateCount).toBe(1); });
    expect(assign).toHaveBeenCalledWith('/admin/orgs');
  });

  test('no impersonation banner for a real signed-in user', async () => {
    renderLayout();
    await screen.findByText('overview body');
    expect(screen.queryByText(/as a platform admin/i)).not.toBeInTheDocument();
  });

  test('theme toggle flips the document theme attribute', async () => {
    localStorage.clear(); // light is the default
    const user = userEvent.setup();
    renderLayout();
    await screen.findByText('overview body');
    expect(document.documentElement.getAttribute('data-theme')).toBe('light');

    await user.click(screen.getByRole('button', { name: 'Toggle theme' }));
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark');

    await user.click(screen.getByRole('button', { name: 'Toggle theme' }));
    expect(document.documentElement.getAttribute('data-theme')).toBe('light');
  });
});
