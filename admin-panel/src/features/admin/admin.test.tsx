import { describe, expect, test } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { FakePanelRepository } from '@/test/fakePanelRepository';
import { buildPanelState } from '@/test/fixtures';
import {
  buildAdminOrg,
  buildAdminOrgLead,
  buildAdminOrgRun,
  buildAdminRunActivity,
  buildAdminRunEvent,
  buildAdminSession,
  buildAuditEntry,
  buildEnrolmentToken,
  buildWorker,
  renderAdmin,
} from './adminTestUtils';
import { EMPTY_ADMIN_RUN_ACTIVITY, mergeAdminRunActivity } from './adminHooks';
import { formatRunDuration } from './format';

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

  test('a box parked by its own preflight reads PARKED, never healthy', async () => {
    // F9.1/F9.2's whole point: this worker is `online` with a fresh heartbeat and looks
    // perfect in every other column. The Health cell is the only thing that says it can
    // take no work, and which check to go fix.
    const user = userEvent.setup();
    const repo = makeRepo();
    repo.adminSession = buildAdminSession();
    repo.fleet = [buildWorker({
      id: 'wrk-parked', displayName: 'ops-pc-3', capabilities: [],
      preflight: {
        ok: false, blocking: true, enforced: true, ranAt: 1_786_800_000,
        failed: [{
          id: 'capabilities', severity: 'fatal', status: 'fail',
          detail: 'neither AIZU_WORKER_PLATFORMS nor AIZU_WORKER_CAPABILITIES is set',
        }],
      },
    })];

    renderAdmin(repo, '/admin');

    await screen.findByText('ops-pc-3');
    await user.click(await screen.findByText('parked · capabilities'));

    expect(screen.getByText(/neither AIZU_WORKER_PLATFORMS/)).toBeInTheDocument();
    // The remedy is resolved client-side from the id — it never rides the wire.
    expect(screen.getByText(/Set AIZU_WORKER_PLATFORMS=all/)).toBeInTheDocument();
  });

  test('an unknown row says "could not check" and offers no misleading remedy', async () => {
    // Chrome being down is the commonest red state, and it marks every login.* row
    // `unknown`. Telling an admin to go finish a login would send them to fix something
    // that was never broken, on a PC nobody can SSH into.
    const user = userEvent.setup();
    const repo = makeRepo();
    repo.adminSession = buildAdminSession();
    repo.fleet = [buildWorker({
      id: 'wrk-nochrome', displayName: 'ops-pc-4',
      preflight: {
        ok: false, blocking: true, enforced: true, ranAt: 1_786_800_000,
        failed: [
          { id: 'cdp_reachable', severity: 'fatal', status: 'fail', detail: 'nothing answers' },
          {
            id: 'login.instagram', severity: 'warn', status: 'unknown',
            detail: 'skipped — CDP endpoint is unreachable',
          },
        ],
      },
    })];

    renderAdmin(repo, '/admin');

    await screen.findByText('ops-pc-4');
    await user.click(await screen.findByText('parked · cdp_reachable'));

    expect(screen.getByText('could not check')).toBeInTheDocument();
    expect(screen.queryByText(/finish the login and any 2FA/)).not.toBeInTheDocument();
    // ...while the check that IS red still carries its instruction.
    expect(screen.getByText(/repoint AIZU_CDP_URL/)).toBeInTheDocument();
  });

  test('a worker with no preflight reported reads as unknown, not as ready', async () => {
    const repo = makeRepo();
    repo.adminSession = buildAdminSession();
    repo.fleet = [buildWorker({ displayName: 'ops-pc-5', preflight: null })];

    renderAdmin(repo, '/admin');

    await screen.findByText('ops-pc-5');
    expect(screen.getByText('—')).toBeInTheDocument();
    expect(screen.queryByText('ready')).not.toBeInTheDocument();
  });

  test('a healthy box reads ready', async () => {
    const repo = makeRepo();
    repo.adminSession = buildAdminSession();
    repo.fleet = [buildWorker({
      displayName: 'ops-pc-6',
      preflight: {
        ok: true, blocking: false, enforced: true, ranAt: 1_786_800_000, failed: [],
      },
    })];

    renderAdmin(repo, '/admin');

    await screen.findByText('ops-pc-6');
    expect(screen.getByText('ready')).toBeInTheDocument();
  });

  test('worker-authored detail is rendered as text, never as markup', async () => {
    // `detail` is a string an off-cloud box chose, landing on the superadmin surface
    // (E1/E2/F18). React escapes it; this asserts nobody swapped in dangerouslySetInnerHTML.
    const user = userEvent.setup();
    const repo = makeRepo();
    repo.adminSession = buildAdminSession();
    const payload = '<img src=x onerror=alert(1)>';
    repo.fleet = [buildWorker({
      displayName: 'ops-pc-7',
      preflight: {
        ok: false, blocking: true, enforced: true, ranAt: 1,
        failed: [{ id: 'cdp_reachable', severity: 'fatal', status: 'fail', detail: payload }],
      },
    })];

    renderAdmin(repo, '/admin');

    await screen.findByText('ops-pc-7');
    await user.click(await screen.findByText('parked · cdp_reachable'));

    expect(screen.getByText(payload)).toBeInTheDocument();
    expect(document.querySelector('img')).toBeNull();
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

describe('org run log (v27 — the feed the customer app lost)', () => {
  test('lists an org’s runs and opens one full narrative feed', async () => {
    const user = userEvent.setup();
    const repo = makeRepo();
    repo.adminSession = buildAdminSession();
    repo.adminOrgRuns = {
      7: [buildAdminOrgRun({ runId: 'run-7a', campaignName: 'Acme sneakers', leads: 5 })],
    };
    repo.adminRunActivity = {
      'run-7a': buildAdminRunActivity({
        runId: 'run-7a',
        events: [
          buildAdminRunEvent({ id: 1, level: 'info', phase: 'lifecycle', message: 'Run started' }),
          buildAdminRunEvent({ id: 2, message: 'Lead: @buyer_42 (score 0.91)' }),
        ],
      }),
    };

    renderAdmin(repo, '/admin/orgs/7');

    await user.click(await screen.findByRole('button', { name: /Acme sneakers/ }));

    // The narrative rows themselves — messages, not just counters.
    expect(await screen.findByText('Run started')).toBeInTheDocument();
    expect(screen.getByText('Lead: @buyer_42 (score 0.91)')).toBeInTheDocument();
    // …plus the aggregated counters that ride alongside them.
    expect(screen.getByText('$0.42')).toBeInTheDocument();
  });

  test('pages the feed forward on the monotonic cursor, never on seq', async () => {
    // `seq` RESETS per session, so a batch run's second session replays 1,2,3…. Paging on
    // the global id is what keeps the console from dropping the whole second session.
    const user = userEvent.setup();
    const repo = makeRepo();
    repo.adminSession = buildAdminSession();
    repo.adminOrgRuns = { 7: [buildAdminOrgRun({ runId: 'run-7b' })] };
    repo.adminRunActivity = {
      'run-7b': (after: number) =>
        after === 0
          ? buildAdminRunActivity({
            runId: 'run-7b', finished: false, cursor: 40,
            events: [buildAdminRunEvent({
              id: 40, seq: 3, sessionId: 'sess-1', message: 'page one tail',
            })],
          })
          : buildAdminRunActivity({
            runId: 'run-7b', finished: true, cursor: 41,
            events: [buildAdminRunEvent({
              // seq goes BACKWARDS (new session) while the global id goes forward.
              id: 41, seq: 1, sessionId: 'sess-2', message: 'page two head',
            })],
          }),
    };

    renderAdmin(repo, '/admin/orgs/7');
    await user.click(await screen.findByRole('button', { name: /Acme sneakers/ }));

    expect(await screen.findByText('page two head')).toBeInTheDocument();
    // The first page's row is still there — pages accumulate, they do not replace.
    expect(screen.getByText('page one tail')).toBeInTheDocument();
    await waitFor(() => {
      expect(repo.adminRunActivityFetches.map((q) => q.after)).toContain(40);
    });
  });

  test('shows the raw engine detail blob on demand', async () => {
    // The detail is exactly what the org endpoint refuses to serve: `{username, score,
    // tier, reelId}`. Seeing it is the point of this plane.
    const user = userEvent.setup();
    const repo = makeRepo();
    repo.adminSession = buildAdminSession();
    repo.adminOrgRuns = { 7: [buildAdminOrgRun({ runId: 'run-7c' })] };
    repo.adminRunActivity = {
      'run-7c': buildAdminRunActivity({
        runId: 'run-7c',
        events: [buildAdminRunEvent({ id: 9, detail: '{"username": "buyer_42"}' })],
      }),
    };

    renderAdmin(repo, '/admin/orgs/7');
    await user.click(await screen.findByRole('button', { name: /Acme sneakers/ }));
    await user.click(await screen.findByRole('button', { name: /^detail$/ }));

    expect(screen.getByText(/"username": "buyer_42"/)).toBeInTheDocument();
  });

  test('an unparseable detail is shown raw rather than swallowed', async () => {
    const user = userEvent.setup();
    const repo = makeRepo();
    repo.adminSession = buildAdminSession();
    repo.adminOrgRuns = { 7: [buildAdminOrgRun({ runId: 'run-7d' })] };
    repo.adminRunActivity = {
      'run-7d': buildAdminRunActivity({
        runId: 'run-7d',
        events: [buildAdminRunEvent({ id: 9, detail: 'not json {' })],
      }),
    };

    renderAdmin(repo, '/admin/orgs/7');
    await user.click(await screen.findByRole('button', { name: /Acme sneakers/ }));
    await user.click(await screen.findByRole('button', { name: /^detail$/ }));

    expect(screen.getByText('not json {')).toBeInTheDocument();
  });

  test('engine-authored message text is rendered as text, never as markup', async () => {
    // Same posture as the worker-authored preflight detail: this is a privileged surface,
    // and the strings on it were written elsewhere.
    const user = userEvent.setup();
    const repo = makeRepo();
    repo.adminSession = buildAdminSession();
    repo.adminOrgRuns = { 7: [buildAdminOrgRun({ runId: 'run-7e' })] };
    const payload = '<img src=x onerror=alert(1)>';
    repo.adminRunActivity = {
      'run-7e': buildAdminRunActivity({
        runId: 'run-7e',
        events: [buildAdminRunEvent({ id: 9, message: payload, detail: null })],
      }),
    };

    renderAdmin(repo, '/admin/orgs/7');
    await user.click(await screen.findByRole('button', { name: /Acme sneakers/ }));

    expect(await screen.findByText(payload)).toBeInTheDocument();
    expect(document.querySelector('img')).toBeNull();
  });

  test('a run with no events yet says so instead of reading as empty', async () => {
    const user = userEvent.setup();
    const repo = makeRepo();
    repo.adminSession = buildAdminSession();
    repo.adminOrgRuns = {
      7: [buildAdminOrgRun({ runId: 'run-7f', status: 'running', finishedAt: null })],
    };
    // Unseeded → the fake answers an empty, unfinished feed, like a fleet run that has
    // not heartbeated its first event.

    renderAdmin(repo, '/admin/orgs/7');
    await user.click(await screen.findByRole('button', { name: /Acme sneakers/ }));

    expect(await screen.findByText(/waiting for the first event/i)).toBeInTheDocument();
  });
});

describe('admin run activity accumulator', () => {
  test('folds successive pages by global id and drops replayed rows', () => {
    const first = mergeAdminRunActivity(
      EMPTY_ADMIN_RUN_ACTIVITY,
      buildAdminRunActivity({
        finished: false, cursor: 2,
        events: [buildAdminRunEvent({ id: 1 }), buildAdminRunEvent({ id: 2 })],
      }),
    );
    expect(first.events.map((e) => e.id)).toEqual([1, 2]);
    expect(first.cursor).toBe(2);
    expect(first.draining).toBe(true);

    // A page the bridge replays (same rows, cursor unmoved) adds nothing AND stops the
    // drain — otherwise the console would re-request the same page forever.
    const replay = mergeAdminRunActivity(
      first,
      buildAdminRunActivity({
        finished: false, cursor: 2,
        events: [buildAdminRunEvent({ id: 1 }), buildAdminRunEvent({ id: 2 })],
      }),
    );
    expect(replay.events.map((e) => e.id)).toEqual([1, 2]);
    expect(replay.draining).toBe(false);
  });

  test('a page for a different run resets the accumulator', () => {
    const acc = mergeAdminRunActivity(
      EMPTY_ADMIN_RUN_ACTIVITY,
      buildAdminRunActivity({ runId: 'run-a', events: [buildAdminRunEvent({ id: 7 })] }),
    );
    const switched = mergeAdminRunActivity(
      acc,
      buildAdminRunActivity({ runId: 'run-b', events: [buildAdminRunEvent({ id: 1 })] }),
    );
    expect(switched.runId).toBe('run-b');
    expect(switched.events.map((e) => e.id)).toEqual([1]);
    expect(switched.cursor).toBe(1);
  });
});

describe('superadmin leads table', () => {
  test('shows the handle, the raw comment and the derived intent side by side', async () => {
    const repo = makeRepo();
    repo.adminSession = buildAdminSession();
    repo.adminOrgLeads = {
      7: {
        leads: [buildAdminOrgLead({
          username: 'buyer_42',
          text: 'do you have these in size 42? dm me',
          intent: 'Wants sneakers in size 42',
        })],
        page: 1, pageSize: 15, total: 1,
      },
    };

    renderAdmin(repo, '/admin/orgs/7');

    expect(await screen.findByText('@buyer_42')).toBeInTheDocument();
    expect(screen.getByText('do you have these in size 42? dm me')).toBeInTheDocument();
    expect(screen.getByText('Wants sneakers in size 42')).toBeInTheDocument();
    // The surface says out loud that this is not what the customer sees.
    expect(screen.getByText(/un-redacted/i)).toBeInTheDocument();
  });

  test('a pre-v27 row with no intent says so and never falls back to the raw text', async () => {
    const repo = makeRepo();
    repo.adminSession = buildAdminSession();
    repo.adminOrgLeads = {
      7: {
        leads: [buildAdminOrgLead({ intent: '', text: 'ship to Tashkent?' })],
        page: 1, pageSize: 15, total: 1,
      },
    };

    renderAdmin(repo, '/admin/orgs/7');

    expect(await screen.findByText('Intent not captured')).toBeInTheDocument();
    // The raw comment still appears exactly once — in its own column, not doubled into
    // the intent cell.
    expect(screen.getAllByText('ship to Tashkent?')).toHaveLength(1);
  });
});

describe('run duration formatting', () => {
  test('reports a duration only when both ends are real', () => {
    expect(formatRunDuration(1_700_000_000, 1_700_000_045)).toBe('45s');
    expect(formatRunDuration(1_700_000_000, 1_700_000_600)).toBe('10m 0s');
    expect(formatRunDuration(1_700_000_000, 1_700_007_800)).toBe('2h 10m');
    // Still running: no end, so no duration — never the browser clock's guess.
    expect(formatRunDuration(1_700_000_000, null)).toBe('—');
    expect(formatRunDuration(null, 1_700_000_600)).toBe('—');
  });
});

describe('dead-lettered run (contract E.5)', () => {
  test('never renders a bare 0 leads beside a feed that shows leads being found', async () => {
    // Measured shape: the job dead-lettered at attempt 5/5 and never acked, so the cloud
    // has no sessions and no matches rows — counters read 0 — while the heartbeat-synced
    // events show every lead the worker harvested. Showing the 0 alone denies real work.
    const user = userEvent.setup();
    const repo = makeRepo();
    repo.adminSession = buildAdminSession();
    repo.adminOrgRuns = { 7: [buildAdminOrgRun({ runId: 'run-dead', status: 'failed', leads: 0 })] };
    repo.adminRunActivity = {
      'run-dead': buildAdminRunActivity({
        runId: 'run-dead',
        finished: true,
        counters: {
          reelsSeen: 0, relevancePasses: 0, commentsScored: 0,
          matches: 0, spendUsd: 0, likes: 0, follows: 0,
        },
        events: [
          buildAdminRunEvent({ id: 1, detail: '{"username": "a", "reelId": "r-1"}' }),
          buildAdminRunEvent({ id: 2, detail: '{"username": "b", "reelId": "r-1"}' }),
          // Attempt 2 re-scores the same comment: same (reelId, username) → one lead, not two.
          buildAdminRunEvent({ id: 3, detail: '{"username": "a", "reelId": "r-1"}' }),
        ],
      }),
    };

    renderAdmin(repo, '/admin/orgs/7');
    await user.click(await screen.findByRole('button', { name: /Acme sneakers/ }));

    expect(await screen.findByRole('status')).toHaveTextContent(
      /2 leads discovered, 0 reached the account/,
    );
  });

  test('stays quiet while the run is still in flight', async () => {
    // Mid-run the session rows legitimately lag the events; flashing the warning at every
    // healthy run would train operators to ignore it.
    const user = userEvent.setup();
    const repo = makeRepo();
    repo.adminSession = buildAdminSession();
    repo.adminOrgRuns = { 7: [buildAdminOrgRun({ runId: 'run-live', status: 'running' })] };
    repo.adminRunActivity = {
      'run-live': buildAdminRunActivity({
        runId: 'run-live',
        finished: false,
        counters: {
          reelsSeen: 3, relevancePasses: 1, commentsScored: 8,
          matches: 0, spendUsd: 0, likes: 0, follows: 0,
        },
        events: [buildAdminRunEvent({ id: 1, detail: '{"username": "a", "reelId": "r-1"}' })],
      }),
    };

    renderAdmin(repo, '/admin/orgs/7');
    await user.click(await screen.findByRole('button', { name: /Acme sneakers/ }));

    expect(await screen.findByText('Lead: @buyer_42 (score 0.91)')).toBeInTheDocument();
    expect(screen.queryByText(/reached the account/)).not.toBeInTheDocument();
  });
});
