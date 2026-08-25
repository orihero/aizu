import { render } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { AppProviders } from '@/app/providers';
import type { FakePanelRepository } from '@/test/fakePanelRepository';
import type {
  AdminOrg,
  AdminOrgLead,
  AdminOrgRun,
  AdminRunActivity,
  AdminRunEvent,
  AdminSession,
  AuditEntry,
  ControlFlag,
  EnrolmentToken,
  FleetWorker,
} from '@/shared/schemas/admin';
import { AdminAuthProvider } from './useAdminAuth';
import { RedirectIfAdmin, RequireSuper } from './AdminGuards';
import { AdminLayout } from './AdminLayout';
import { AdminLoginPage } from './AdminLoginPage';
import { FleetPage } from './FleetPage';
import { OrgsPage } from './OrgsPage';
import { OrgDetailPage } from './OrgDetailPage';
import { AuditPage } from './AuditPage';
import { ModelPerformancePage } from './ModelPerformancePage';

/**
 * Render the `/admin` route subtree exactly as router.tsx wires it (provider →
 * guards → layout → pages), inside AppProviders with a fake repository. Mirrors
 * renderWithProviders but for the separate superadmin plane's nested routes.
 */
export function renderAdmin(repository: FakePanelRepository, initialPath = '/admin') {
  return render(
    <AppProviders repository={repository}>
      <MemoryRouter initialEntries={[initialPath]}>
        <Routes>
          <Route path="/admin" element={<AdminAuthProvider />}>
            <Route element={<RedirectIfAdmin />}>
              <Route path="login" element={<AdminLoginPage />} />
            </Route>
            <Route element={<RequireSuper />}>
              <Route element={<AdminLayout />}>
                <Route index element={<FleetPage />} />
                <Route path="orgs" element={<OrgsPage />} />
                <Route path="orgs/:orgId" element={<OrgDetailPage />} />
                <Route path="audit" element={<AuditPage />} />
                <Route path="model-performance" element={<ModelPerformancePage />} />
              </Route>
            </Route>
          </Route>
          {/* Stand-in for the org root so post-logout / cross-plane redirects have a target. */}
          <Route path="/login" element={<div>org login</div>} />
        </Routes>
      </MemoryRouter>
    </AppProviders>,
  );
}

export function buildAdminSession(overrides: Partial<AdminSession> = {}): AdminSession {
  return {
    id: 1,
    email: 'ops@aizu.test',
    impersonating: false,
    effectiveOrgId: null,
    effectiveUserId: null,
    impersonationReason: null,
    ...overrides,
  };
}

export function buildWorker(overrides: Partial<FleetWorker> = {}): FleetWorker {
  return {
    id: 'wrk-abc123',
    orgId: 1,
    displayName: 'studio-mac',
    host: 'studio-mac.local',
    os: 'macOS 15',
    agentVersion: '1.4.2',
    maxSessions: 2,
    currentSessions: 1,
    capabilities: [[1, 'instagram', 'acme']],
    registeredAt: 1_700_000_000,
    lastHeartbeatAt: 1_700_000_100,
    lastSeenAgeSec: 12,
    status: 'online',
    revokedAt: null,
    currentJob: null,
    // Default null = "this box has never reported a preflight", which the Health cell
    // renders as "—". Deliberately NOT a green summary: a worker the server has no report
    // for has not been cleared of anything, and a fixture that defaulted to healthy would
    // let a regression in that distinction pass every test.
    preflight: null,
    ...overrides,
  };
}

export function buildControlFlag(overrides: Partial<ControlFlag> = {}): ControlFlag {
  return {
    scope: 'global',
    scopeKey: '',
    drain: false,
    halt: true,
    updateRequired: false,
    reason: 'incident',
    setBy: 'ops@aizu.test',
    updatedAt: 1_700_000_200,
    ...overrides,
  };
}

export function buildEnrolmentToken(overrides: Partial<EnrolmentToken> = {}): EnrolmentToken {
  return {
    id: 'wet-abc123',
    scopeKind: 'org',
    orgId: 7,
    label: 'studio-mac',
    createdAt: 1_700_000_000,
    createdByAdminId: 1,
    expiresAt: 1_700_604_800,
    redeemedAt: null,
    redeemedByWorkerId: null,
    revokedAt: null,
    revokedByAdminId: null,
    ...overrides,
  };
}

export function buildAdminOrg(overrides: Partial<AdminOrg> = {}): AdminOrg {
  return {
    id: 7,
    name: 'Acme Inc',
    logo: null,
    description: null,
    createdAt: 1_699_000_000,
    memberCount: 4,
    campaignCount: 3,
    ...overrides,
  };
}

export function buildAuditEntry(overrides: Partial<AuditEntry> = {}): AuditEntry {
  return {
    id: 42,
    prevHash: 'aa',
    rowHash: 'bb',
    actingAdminId: 1,
    action: 'impersonate.start',
    targetOrgId: 7,
    targetUserId: null,
    targetResource: null,
    at: 1_700_000_300,
    ip: '10.0.0.4',
    userAgent: 'test',
    reason: 'support ticket 91',
    impersonationStart: 1_700_000_300,
    impersonationEnd: null,
    ...overrides,
  };
}

export function buildAdminOrgRun(overrides: Partial<AdminOrgRun> = {}): AdminOrgRun {
  return {
    runId: 'run-abc',
    campaignId: 'c-acme',
    campaignName: 'Acme sneakers',
    mode: 'live',
    status: 'done',
    platforms: ['instagram'],
    startedAt: 1_700_000_000,
    finishedAt: 1_700_000_600,
    sessions: 2,
    leads: 5,
    ...overrides,
  };
}

export function buildAdminRunEvent(overrides: Partial<AdminRunEvent> = {}): AdminRunEvent {
  return {
    id: 1,
    seq: 1,
    campaignId: 'c-acme',
    sessionId: 'sess-1',
    phase: 'comments',
    level: 'success',
    message: 'Lead: @buyer_42 (score 0.91)',
    detail: '{"username": "buyer_42", "score": 0.91, "tier": "hot", "reelId": "r-1"}',
    createdAt: 1_700_000_100,
    platform: 'instagram',
    ...overrides,
  };
}

export function buildAdminRunActivity(
  overrides: Partial<AdminRunActivity> = {},
): AdminRunActivity {
  const events = overrides.events ?? [buildAdminRunEvent()];
  return {
    runId: 'run-abc',
    finished: true,
    counters: {
      reelsSeen: 12, relevancePasses: 5, commentsScored: 40,
      matches: 3, spendUsd: 0.42, likes: 2, follows: 1,
    },
    flags: [],
    // Default the cursor to the last event's id, which is what the bridge echoes back —
    // a fixture whose cursor lags its own rows would make the drain loop re-request them.
    cursor: events.at(-1)?.id ?? 0,
    ...overrides,
    events,
  };
}

export function buildAdminOrgLead(overrides: Partial<AdminOrgLead> = {}): AdminOrgLead {
  return {
    commentId: 'cm-1',
    campaignId: 'c-acme',
    platform: 'instagram',
    username: 'buyer_42',
    text: 'do you have these in size 42? dm me',
    intent: 'Wants red sneakers in size 42, asking on a running-shoes post',
    capturedAt: 1_700_000_100,
    status: 'new',
    score: 0.91,
    reason: 'explicit purchase intent',
    extracted: true,
    tier: 'hot',
    ...overrides,
  };
}
