import { describe, expect, test } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { AppProviders } from '@/app/providers';
import { FakePanelRepository } from '@/test/fakePanelRepository';
import { buildCampaign, buildPanelState, buildRunBlock } from '@/test/fixtures';
import type { Role } from '@/shared/auth/roles';
import { RunDrawer } from './RunDrawer';

function renderDrawer(repository: FakePanelRepository, role: Role) {
  repository.currentUser = repository.currentUser && { ...repository.currentUser, role };
  const campaign = buildCampaign({ id: 'cmp-001' });
  render(
    <AppProviders repository={repository}>
      <RunDrawer campaign={campaign} run={buildRunBlock()} isOpen onClose={() => {}} />
    </AppProviders>,
  );
}

describe('RunDrawer run-start error handling', () => {
  test('a 409 agent-not-ready failure names the problem and points an admin at the fix banner', async () => {
    const user = userEvent.setup();
    const repository = new FakePanelRepository(buildPanelState());
    repository.runStartFailure = 'agent_not_ready';
    repository.runStartAgentNotReadyDetail =
      'Chrome (CDP) unreachable — launch the login browser first.';
    renderDrawer(repository, 'admin');

    await user.click(screen.getByRole('button', { name: /start run/i }));

    await screen.findByText(/Chrome \(CDP\) unreachable/i);
    expect(screen.getByText(/"Instagram agent" banner/i)).toBeInTheDocument();
    // The write itself must NOT have been recorded — the gate rejected it.
    expect(repository.runRequests).toHaveLength(0);
  });

  test('the same failure tells a role without fix_agent that an admin must fix it', async () => {
    const user = userEvent.setup();
    const repository = new FakePanelRepository(buildPanelState());
    repository.runStartFailure = 'agent_not_ready';
    repository.runStartAgentNotReadyDetail = 'Instagram session logged out.';
    renderDrawer(repository, 'viewer');

    await user.click(screen.getByRole('button', { name: /start run/i }));

    await screen.findByText(/Instagram session logged out/i);
    expect(screen.getByText(/administrator needs to fix this/i)).toBeInTheDocument();
  });

  test('a plain run-conflict error shows only the server message, no fix-banner hint', async () => {
    const user = userEvent.setup();
    const repository = new FakePanelRepository(buildPanelState());
    repository.failNextWrite = true; // FakePanelRepository's generic simulated failure
    renderDrawer(repository, 'admin');

    await user.click(screen.getByRole('button', { name: /start run/i }));

    await screen.findByText(/simulated write failure/i);
    expect(screen.queryByText(/administrator needs to fix this/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/"Instagram agent" banner/i)).not.toBeInTheDocument();
  });
});
