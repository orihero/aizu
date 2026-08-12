import { describe, expect, test } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { FakePanelRepository } from '@/test/fakePanelRepository';
import { buildPanelState } from '@/test/fixtures';
import {
  buildAdminOrg,
  buildAdminSession,
  buildAuditEntry,
  buildEnrolmentToken,
  buildWorker,
  renderAdmin,
} from './adminTestUtils';

function makeRepo(): FakePanelRepository {
  return new FakePanelRepository(buildPanelState());
}

describe('RequireSuper gate', () => {
  test('bounces an anonymous visitor from the console to the admin login', async () => {
    const repo = makeRepo();
    repo.adminSession = null;

    renderAdmin(repo, '/admin');

    expect(await screen.findByText(/platform admin sign-in/i)).toBeInTheDocument();
  });

  test('renders the fleet for a live admin session', async () => {
    const repo = makeRepo();
    repo.adminSession = buildAdminSession();
    repo.fleet = [buildWorker()];

    renderAdmin(repo, '/admin');

    expect(await screen.findByRole('heading', { name: 'Fleet' })).toBeInTheDocument();
    expect(await screen.findByText('studio-mac')).toBeInTheDocument();
  });
});

describe('admin login', () => {
  test('submits email + password + a 6-digit code and lands on the fleet', async () => {
    const user = userEvent.setup();
    const repo = makeRepo();
    repo.adminSession = null; // start signed out
    repo.fleet = [buildWorker()];

    renderAdmin(repo, '/admin/login');

    await user.type(await screen.findByLabelText(/email/i), 'ops@aizu.test');
    await user.type(screen.getByLabelText(/password/i), 'hunter2hunter2');
    // Non-digits are stripped; only 6 digits are kept.
    await user.type(screen.getByLabelText(/authenticator code/i), '12ab3456');
    await user.click(screen.getByRole('button', { name: /sign in/i }));

    await waitFor(() => { expect(repo.adminLoginAttempts).toHaveLength(1); });
    expect(repo.adminLoginAttempts[0]).toEqual({
      email: 'ops@aizu.test',
      password: 'hunter2hunter2',
      totpCode: '123456',
    });
    // login flips the session → the guard now admits the console.
    expect(await screen.findByRole('heading', { name: 'Fleet' })).toBeInTheDocument();
  });

  test('shows a throttle message on HTTP 429', async () => {
    const user = userEvent.setup();
    const repo = makeRepo();
    repo.adminSession = null;
    repo.failNextAdmin = { message: 'locked', status: 429 };

    renderAdmin(repo, '/admin/login');

    await user.type(await screen.findByLabelText(/email/i), 'ops@aizu.test');
    await user.type(screen.getByLabelText(/password/i), 'hunter2hunter2');
    await user.type(screen.getByLabelText(/authenticator code/i), '123456');
    await user.click(screen.getByRole('button', { name: /sign in/i }));

    expect(await screen.findByRole('alert')).toHaveTextContent(/too many attempts/i);
  });
});

describe('fleet actions', () => {
  test('revokes a worker token after confirmation', async () => {
    const user = userEvent.setup();
    const repo = makeRepo();
    repo.adminSession = buildAdminSession();
    repo.fleet = [buildWorker({ id: 'wrk-xyz', displayName: 'box-1' })];

    renderAdmin(repo, '/admin');

    await screen.findByText('box-1');
    await user.click(screen.getByRole('button', { name: /revoke/i }));

    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: /^revoke$/i }));

    await waitFor(() => { expect(repo.workerRevokes).toEqual(['wrk-xyz']); });
  });

  test('shows the current job each worker is running (and idle otherwise)', async () => {
    const repo = makeRepo();
    repo.adminSession = buildAdminSession();
    repo.fleet = [
      buildWorker({
        id: 'wrk-busy', displayName: 'box-busy',
        currentJob: {
          jobId: 'job-1', campaignId: 'c-acme', platform: 'instagram',
          status: 'running', runId: 'run-xyz', leaseExpiresAt: 1_700_000_500,
        },
      }),
      buildWorker({ id: 'wrk-idle', displayName: 'box-idle', currentJob: null }),
    ];

    renderAdmin(repo, '/admin');

    await screen.findByText('box-busy');
    expect(screen.getByText('c-acme')).toBeInTheDocument();
    expect(screen.getByText(/instagram · running/i)).toBeInTheDocument();
    expect(screen.getByText('idle')).toBeInTheDocument();
  });

  test('sets a global halt control flag', async () => {
    const user = userEvent.setup();
    const repo = makeRepo();
    repo.adminSession = buildAdminSession();
    repo.fleet = [buildWorker()];
    repo.controlFlags = [];

    renderAdmin(repo, '/admin');

    await user.click(await screen.findByRole('button', { name: /new flag/i }));
    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByLabelText(/halt/i));
    await user.click(within(dialog).getByRole('button', { name: /set flag/i }));

    await waitFor(() => { expect(repo.controlFlagWrites).toHaveLength(1); });
    expect(repo.controlFlagWrites[0]).toMatchObject({ scope: 'global', halt: true });
  });
});

describe('enrolment tokens', () => {
  test('mints an org-scoped token and shows the plaintext once', async () => {
    const user = userEvent.setup();
    const repo = makeRepo();
    repo.adminSession = buildAdminSession();
    repo.fleet = [buildWorker()];
    repo.adminOrgs = [buildAdminOrg({ id: 7, name: 'Acme Inc' })];

    renderAdmin(repo, '/admin');

    await user.click(await screen.findByRole('button', { name: /^mint token$/i }));
    const dialog = await screen.findByRole('dialog');
    await user.selectOptions(within(dialog).getByLabelText(/^org$/i), '7');
    await user.click(within(dialog).getByRole('button', { name: /^mint token$/i }));

    await waitFor(() => { expect(repo.enrolmentTokenMints).toHaveLength(1); });
    expect(repo.enrolmentTokenMints[0]).toMatchObject({ scope: 'org', orgId: 7 });
    // The plaintext token is shown exactly once, in a copy-once field.
    expect(within(dialog).getByLabelText('Enrolment token')).toHaveValue(
      'fake-plaintext-token-wet-fake-1',
    );
  });

  test('revokes a pending enrolment token after confirmation', async () => {
    const user = userEvent.setup();
    const repo = makeRepo();
    repo.adminSession = buildAdminSession();
    repo.fleet = [];
    repo.enrolmentTokens = [buildEnrolmentToken({ id: 'wet-xyz', label: 'wet-row-1' })];

    renderAdmin(repo, '/admin');

    await screen.findByText('wet-row-1');
    await user.click(screen.getByRole('button', { name: /revoke/i }));

    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: /^revoke$/i }));

    await waitFor(() => { expect(repo.enrolmentTokenRevokes).toEqual(['wet-xyz']); });
  });
});

describe('organizations + impersonation', () => {
  test('lists orgs and starts a reason-gated impersonation with a visible banner', async () => {
    const user = userEvent.setup();
    const repo = makeRepo();
    repo.adminSession = buildAdminSession();
    repo.adminOrgs = [buildAdminOrg({ id: 7, name: 'Acme Inc' })];

    renderAdmin(repo, '/admin/orgs/7');

    await user.click(await screen.findByRole('button', { name: /^impersonate$/i }));
    const dialog = await screen.findByRole('dialog');
    await user.type(within(dialog).getByLabelText(/reason/i), 'support ticket 91');
    await user.click(within(dialog).getByRole('button', { name: /start impersonation/i }));

    await waitFor(() => { expect(repo.impersonateRequests).toHaveLength(1); });
    expect(repo.impersonateRequests[0]).toEqual({ orgId: 7, reason: 'support ticket 91' });
    // The fake flips the session to impersonating → the global banner shows.
    expect(await screen.findByText(/impersonating org #7/i)).toBeInTheDocument();
  });

  test('ends an active impersonation from the banner', async () => {
    const user = userEvent.setup();
    const repo = makeRepo();
    repo.adminSession = buildAdminSession({
      impersonating: true,
      effectiveOrgId: 7,
      impersonationReason: 'support ticket 91',
    });
    repo.fleet = [buildWorker()];

    renderAdmin(repo, '/admin');

    await user.click(await screen.findByRole('button', { name: /end impersonation/i }));

    await waitFor(() => { expect(repo.endImpersonateCount).toBe(1); });
  });

  test('offers an Open workspace hand-off while impersonating (Gap F)', async () => {
    const repo = makeRepo();
    repo.adminSession = buildAdminSession({
      impersonating: true, effectiveOrgId: 7, impersonationReason: 'support',
    });
    repo.fleet = [buildWorker()];

    renderAdmin(repo, '/admin');

    // The cross-plane hand-off button into the org app is present during impersonation.
    expect(await screen.findByRole('button', { name: /open workspace/i })).toBeInTheDocument();
  });
});

describe('execution-backend switch', () => {
  test('shows the active backend and switches to the fleet after confirming', async () => {
    const user = userEvent.setup();
    const repo = makeRepo();
    repo.adminSession = buildAdminSession();
    repo.fleet = [buildWorker()];
    repo.executionBackend = 'in_process';

    renderAdmin(repo, '/admin');

    // The in-process tile is marked active.
    expect(
      await screen.findByRole('button', { name: /in-process/i, pressed: true }),
    ).toBeInTheDocument();

    // Switching to the fleet is confirmed (it reroutes every run) then applied.
    await user.click(screen.getByRole('button', { name: /worker fleet/i }));
    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: /^switch$/i }));

    await waitFor(() => { expect(repo.executionBackendWrites).toEqual(['distributed']); });
  });
});

describe('model comparison', () => {
  test('shows off by default, lists configured models, and turns on after confirming', async () => {
    const user = userEvent.setup();
    const repo = makeRepo();
    repo.adminSession = buildAdminSession();
    repo.modelComparisonSettings = { enabled: false, models: ['candidate-a', 'candidate-b'] };

    renderAdmin(repo, '/admin/model-performance');

    expect(await screen.findByRole('switch')).toHaveAttribute('aria-checked', 'false');
    expect(screen.getByText(/candidate-a, candidate-b/i)).toBeInTheDocument();

    await user.click(screen.getByRole('switch'));
    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: /turn on/i }));

    await waitFor(() => { expect(repo.modelComparisonWrites).toEqual([true]); });
  });

  test('disables the switch when no comparison models are configured', async () => {
    const repo = makeRepo();
    repo.adminSession = buildAdminSession();
    repo.modelComparisonSettings = { enabled: false, models: [] };

    renderAdmin(repo, '/admin/model-performance');

    expect(await screen.findByRole('switch')).toBeDisabled();
  });

  test('renders aggregate per-model stats', async () => {
    const repo = makeRepo();
    repo.adminSession = buildAdminSession();
    repo.modelComparisonStats = {
      stats: [
        { model: 'prod-model', isPrimary: true, calls: 10, avgLatencyMs: 420, avgUsd: 0.002,
          avgScore: 0.8, agreementRate: null, errors: 0, leadsFound: 3 },
        { model: 'candidate-a', isPrimary: false, calls: 10, avgLatencyMs: 900, avgUsd: 0.0005,
          avgScore: 0.6, agreementRate: 0.7, errors: 1, leadsFound: 2 },
      ],
      recent: [],
    };

    renderAdmin(repo, '/admin/model-performance');

    expect(await screen.findByText('prod-model')).toBeInTheDocument();
    expect(screen.getByText('candidate-a')).toBeInTheDocument();
    expect(screen.getByText('production')).toBeInTheDocument();
    expect(screen.getByText('70%')).toBeInTheDocument();
  });

  test('shows an empty state when nothing has been logged yet', async () => {
    const repo = makeRepo();
    repo.adminSession = buildAdminSession();

    renderAdmin(repo, '/admin/model-performance');

    expect(await screen.findByText(/no comparison calls logged yet/i)).toBeInTheDocument();
  });
});

describe('audit log', () => {
  test('renders entries and reports an intact chain on verify', async () => {
    const user = userEvent.setup();
    const repo = makeRepo();
    repo.adminSession = buildAdminSession();
    repo.auditEntries = [buildAuditEntry({ action: 'impersonate.start', reason: 'ticket 91' })];
    repo.auditVerifyResult = { ok: true, count: 1, firstBadId: null };

    renderAdmin(repo, '/admin/audit');

    expect(await screen.findByText('impersonate.start')).toBeInTheDocument();
    expect(screen.getByText('ticket 91')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /verify chain/i }));
    expect(await screen.findByText(/chain intact/i)).toBeInTheDocument();
  });

  test('flags tampering when verify fails', async () => {
    const user = userEvent.setup();
    const repo = makeRepo();
    repo.adminSession = buildAdminSession();
    repo.auditEntries = [buildAuditEntry()];
    repo.auditVerifyResult = { ok: false, count: 5, firstBadId: 3 };

    renderAdmin(repo, '/admin/audit');

    await user.click(await screen.findByRole('button', { name: /verify chain/i }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/tamper detected/i);
  });
});
