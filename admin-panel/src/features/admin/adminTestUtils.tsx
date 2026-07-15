import { render } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { AppProviders } from '@/app/providers';
import type { FakePanelRepository } from '@/test/fakePanelRepository';
import type {
  AdminOrg,
  AdminSession,
  AuditEntry,
  ControlFlag,
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
