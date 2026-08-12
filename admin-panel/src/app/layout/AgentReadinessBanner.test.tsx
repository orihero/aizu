import { describe, expect, test } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { AppProviders } from '@/app/providers';
import { buildAgentReadiness, buildPanelState } from '@/test/fixtures';
import { FakePanelRepository } from '@/test/fakePanelRepository';
import type { Role } from '@/shared/auth/roles';
import { AgentReadinessBanner } from './AgentReadinessBanner';

function renderBanner(repository: FakePanelRepository, role: Role = 'owner') {
  repository.currentUser = repository.currentUser && { ...repository.currentUser, role };
  render(
    <AppProviders repository={repository}>
      <MemoryRouter>
        <AgentReadinessBanner />
      </MemoryRouter>
    </AppProviders>,
  );
}

describe('AgentReadinessBanner', () => {
  test('renders nothing while the agent is ready', async () => {
    const repository = new FakePanelRepository(buildPanelState());
    repository.agentReadiness = buildAgentReadiness({ ready: true });
    renderBanner(repository, 'owner');
    await waitFor(() => { expect(repository.agentReadinessFetches.length).toBeGreaterThan(0); });
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  test('member (no fix_agent) sees only the informational message, no action buttons', async () => {
    const repository = new FakePanelRepository(buildPanelState());
    repository.agentReadiness = buildAgentReadiness({
      ready: false, cdp: 'unreachable', instagram: 'unknown', detail: 'connect ECONNREFUSED',
    });
    renderBanner(repository, 'member');

    const banner = await screen.findByRole('alert');
    expect(banner).toHaveTextContent(/administrator needs to fix this/i);
    expect(screen.queryByRole('button', { name: /launch login browser/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /re-check/i })).not.toBeInTheDocument();
  });

  test('viewer (no fix_agent) also sees only the informational message', async () => {
    const repository = new FakePanelRepository(buildPanelState());
    repository.agentReadiness = buildAgentReadiness({ ready: false, cdp: 'unreachable' });
    renderBanner(repository, 'viewer');

    const banner = await screen.findByRole('alert');
    expect(banner).toHaveTextContent(/administrator needs to fix this/i);
  });

  test('admin sees the actionable banner naming CDP unreachable', async () => {
    const repository = new FakePanelRepository(buildPanelState());
    repository.agentReadiness = buildAgentReadiness({
      ready: false, cdp: 'unreachable', instagram: 'unknown',
    });
    renderBanner(repository, 'admin');

    const banner = await screen.findByRole('alert');
    expect(banner).toHaveTextContent('Chrome (CDP) unreachable');
    expect(screen.getByRole('button', { name: /launch login browser/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /re-check/i })).toBeInTheDocument();
  });

  test('owner sees the actionable banner naming Instagram logged out when CDP is fine', async () => {
    const repository = new FakePanelRepository(buildPanelState());
    repository.agentReadiness = buildAgentReadiness({
      ready: false, cdp: 'ok', instagram: 'logged_out',
    });
    renderBanner(repository, 'owner');

    const banner = await screen.findByRole('alert');
    expect(banner).toHaveTextContent('Instagram session logged out');
  });

  test('distributed backend names the fleet problem, not Chrome, and offers no launch', async () => {
    // The cloud control plane has no browser: pointing an admin at "Chrome (CDP)
    // unreachable" would send them to the wrong machine, and there is nothing here
    // for "Launch login browser" to open.
    const repository = new FakePanelRepository(buildPanelState());
    repository.agentReadiness = buildAgentReadiness({
      ready: false, cdp: 'unreachable', instagram: 'unknown', backend: 'distributed',
      detail: 'no worker is online — a live run would be queued with nothing to pick it up.',
    });
    renderBanner(repository, 'admin');

    const banner = await screen.findByRole('alert');
    expect(banner).toHaveTextContent(/no worker is online/i);
    expect(banner).not.toHaveTextContent('Chrome (CDP) unreachable');
    expect(screen.queryByRole('button', { name: /launch login browser/i })).not.toBeInTheDocument();
    // Re-check still applies — a worker can come online at any moment.
    expect(screen.getByRole('button', { name: /re-check/i })).toBeInTheDocument();
  });

  test('Launch login browser calls launchAgentLogin and shows feedback', async () => {
    const user = userEvent.setup();
    const repository = new FakePanelRepository(buildPanelState());
    repository.agentReadiness = buildAgentReadiness({ ready: false, cdp: 'unreachable' });
    renderBanner(repository, 'admin');

    await user.click(await screen.findByRole('button', { name: /launch login browser/i }));

    await waitFor(() => { expect(repository.launchAgentLoginCalls).toBe(1); });
    await screen.findByText(/login browser launched/i);
  });

  test('Launch login browser surfaces a failure inline', async () => {
    const user = userEvent.setup();
    const repository = new FakePanelRepository(buildPanelState());
    repository.agentReadiness = buildAgentReadiness({ ready: false, cdp: 'unreachable' });
    repository.failNextAgentLaunch = { message: 'Chrome executable not found', status: 500 };
    renderBanner(repository, 'admin');

    await user.click(await screen.findByRole('button', { name: /launch login browser/i }));

    await screen.findByText(/chrome executable not found/i);
  });

  test('Re-check forces a live probe (refresh:true) rather than the cached read', async () => {
    const user = userEvent.setup();
    const repository = new FakePanelRepository(buildPanelState());
    repository.agentReadiness = buildAgentReadiness({ ready: false, cdp: 'unreachable' });
    renderBanner(repository, 'admin');

    await screen.findByRole('alert');
    const priorFetches = repository.agentReadinessFetches.length;
    await user.click(screen.getByRole('button', { name: /re-check/i }));

    await waitFor(() => {
      expect(repository.agentReadinessFetches.length).toBeGreaterThan(priorFetches);
    });
    expect(repository.agentReadinessFetches.at(-1)).toEqual({ refresh: true });
  });

  test('banner disappears once readiness reports ready (e.g. after a successful login)', async () => {
    const repository = new FakePanelRepository(buildPanelState());
    repository.agentReadiness = buildAgentReadiness({ ready: false, cdp: 'unreachable' });
    renderBanner(repository, 'admin');
    await screen.findByRole('alert');

    repository.agentReadiness = buildAgentReadiness({ ready: true });
    // Drive the next poll manually via Re-check rather than waiting on the real 60s timer.
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /re-check/i }));

    await waitFor(() => { expect(screen.queryByRole('alert')).not.toBeInTheDocument(); });
  });
});
