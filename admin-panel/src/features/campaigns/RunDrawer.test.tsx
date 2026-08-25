import { describe, expect, test } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { FakePanelRepository } from '@/test/fakePanelRepository';
import { buildBilling, buildCampaign, buildPanelState, buildRunBlock } from '@/test/fixtures';
import { renderWithProviders } from '@/test/renderWithProviders';
import type { Billing } from '@/shared/types/domain';
import type { Role } from '@/shared/auth/roles';
import { RunDrawer } from './RunDrawer';

function renderDrawer(repository: FakePanelRepository, role: Role) {
  repository.currentUser = repository.currentUser && { ...repository.currentUser, role };
  const campaign = buildCampaign({ id: 'cmp-001' });
  // MemoryRouter-backed: the plan copy links to /settings/billing.
  renderWithProviders(
    <RunDrawer campaign={campaign} run={buildRunBlock()} isOpen onClose={() => {}} />,
    { repository },
  );
}

/** A billing block with the v27 plan bounds spelled out, so this suite describes the
 *  payload contract rather than inheriting whatever the shared fixture defaults to. */
function planned(overrides: Partial<Billing> = {}): Billing {
  return buildBilling({
    tier: 'free', leadCap: 10, leadsUsed: 0,
    campaignCap: 1, campaignsUsed: 1, maxRunLeads: 10,
    ...overrides,
  });
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
    expect(screen.queryByRole('link', { name: /upgrade plan/i })).not.toBeInTheDocument();
  });
});

describe('RunDrawer plan bounds', () => {
  test('names the plan and its per-run allowance', async () => {
    const repository = new FakePanelRepository(buildPanelState());
    repository.billing = planned({ leadsUsed: 3 });
    renderDrawer(repository, 'admin');

    // "up to N", never "exactly N": a run's stop condition is per-session, so it can
    // overshoot its target (E.6). The remaining period allowance rides along.
    expect(await screen.findByText(/Free plan: up to 7 leads per run/i)).toBeInTheDocument();
    expect(screen.getByText(/7 left this period/i)).toBeInTheDocument();
  });

  test('pressing Start with the defaults produces an in-plan run', async () => {
    // The one-button path. The campaign's goal (25) is over what Free allows, and the
    // free user must not have to solve a form before they can run anything.
    const user = userEvent.setup();
    const repository = new FakePanelRepository(buildPanelState());
    repository.billing = planned();
    renderDrawer(repository, 'admin');

    await screen.findByText(/Free plan: up to 10 leads per run/i);
    await user.click(screen.getByRole('button', { name: /start run/i }));

    await waitFor(() => { expect(repository.runRequests).toHaveLength(1); });
    expect(repository.runRequests[0]?.targetLeadCount).toBe(10);
  });

  test('says out loud when the plan clamped the chosen target', async () => {
    const repository = new FakePanelRepository(buildPanelState());
    repository.billing = planned();
    renderDrawer(repository, 'admin');

    expect(await screen.findByText(/plan caps this run at 10 leads/i)).toBeInTheDocument();
  });

  test('a preset above the plan’s allowance is disabled, not silently accepted', async () => {
    const repository = new FakePanelRepository(buildPanelState());
    repository.billing = planned();
    renderDrawer(repository, 'admin');

    await screen.findByText(/Free plan: up to 10 leads per run/i);
    expect(screen.getByRole('button', { name: '50' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '10' })).toBeEnabled();
  });

  test('an exhausted period blocks Start and offers the upgrade instead of a silent 402', async () => {
    const repository = new FakePanelRepository(buildPanelState());
    repository.billing = planned({ leadsUsed: 10 });
    renderDrawer(repository, 'admin');

    expect(await screen.findByText(/used all 10 leads on Free this billing period/i)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /upgrade plan/i })).toHaveAttribute('href', '/settings/billing');
    expect(screen.getByRole('button', { name: /start run/i })).toBeDisabled();
  });

  test('a bigger plan bounds the run higher and clamps nothing', async () => {
    const repository = new FakePanelRepository(buildPanelState());
    repository.billing = planned({
      tier: 'pro', leadCap: 2000, leadsUsed: 0, maxRunLeads: 2000, campaignCap: null,
    });
    renderDrawer(repository, 'admin');

    expect(await screen.findByText(/Pro plan: up to 2,000 leads per run/i)).toBeInTheDocument();
    expect(screen.queryByText(/plan caps this run/i)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '100' })).toBeEnabled();
  });

  test('an unreported per-run bound falls back to the period cap, never to zero', async () => {
    // `maxRunLeads` is `.catch(0)` at the boundary, so a pre-v27 bridge reads 0. Treating
    // that as a real bound would offer every org a run target of zero.
    const repository = new FakePanelRepository(buildPanelState());
    repository.billing = planned({ maxRunLeads: 0, leadCap: 50, leadsUsed: 0 });
    renderDrawer(repository, 'admin');

    expect(await screen.findByText(/Free plan: up to 50 leads per run/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /start run/i })).toBeEnabled();
  });

  test('the custom lead field is bounded by the plan, and an over-plan entry still clamps', async () => {
    // Two halves of the same rule. The field ADVERTISES the plan's per-run allowance
    // (spinner + browser validity agree with what Start sends), and because `max` on a
    // number input cannot stop someone typing past it, the value that actually leaves
    // the drawer is clamped rather than rejected — the one-button path must keep working.
    const user = userEvent.setup();
    const repository = new FakePanelRepository(buildPanelState());
    repository.billing = planned();
    renderDrawer(repository, 'admin');

    await user.click(await screen.findByRole('button', { name: /custom lead target/i }));
    const leadsField = screen.getByLabelText(/Leads/);
    expect(leadsField).toHaveAttribute('max', '10');

    await user.clear(leadsField);
    await user.type(leadsField, '500');
    expect(await screen.findByText(/plan caps this run at 10 leads/i)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /start run/i }));
    await waitFor(() => { expect(repository.runRequests).toHaveLength(1); });
    // 500 was typed; 10 is what the server is asked for. Never the 500.
    expect(repository.runRequests[0]?.targetLeadCount).toBe(10);
  });

  test('a plan with no per-run bound falls back to the global safety cap, not to zero', async () => {
    const user = userEvent.setup();
    const repository = new FakePanelRepository(buildPanelState());
    repository.billing = undefined;
    renderDrawer(repository, 'admin');

    await user.click(await screen.findByRole('button', { name: /custom lead target/i }));
    const max = screen.getByLabelText(/Leads/).getAttribute('max');
    expect(Number(max)).toBeGreaterThan(10);
  });

  test('unknown billing leaves the form unbounded rather than locked', async () => {
    // A role-pruned or still-loading BILLING must not disable the run: the server is the
    // real gate and it clamps the target itself.
    const user = userEvent.setup();
    const repository = new FakePanelRepository(buildPanelState());
    repository.billing = undefined;
    renderDrawer(repository, 'admin');

    await user.click(screen.getByRole('button', { name: /start run/i }));

    await waitFor(() => { expect(repository.runRequests).toHaveLength(1); });
    expect(repository.runRequests[0]?.targetLeadCount).toBe(25);
    expect(screen.queryByText(/plan: up to/i)).not.toBeInTheDocument();
  });
});
