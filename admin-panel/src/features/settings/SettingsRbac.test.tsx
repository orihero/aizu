import { afterEach, describe, expect, test, vi } from 'vitest';
import { screen } from '@testing-library/react';
import type { Action, Role } from '@/shared/auth/roles';
import { FakePanelRepository } from '@/test/fakePanelRepository';
import { buildPanelState } from '@/test/fixtures';
import { renderWithProviders } from '@/test/renderWithProviders';
import { SettingsPage } from './SettingsPage';

/**
 * RBAC UX coverage for Settings. Two concerns:
 *  1. Regression: owner AND admin (the roles in today's matrix for view_settings,
 *     view_team and toggle_integration) see all three sub-tabs.
 *  2. Source-of-truth: tab visibility is DERIVED from can(), so a future matrix
 *     change is honored — proven by overriding can() and asserting a tab disappears.
 *
 * `can` is mocked so the test controls the policy without a real matrix edit. The
 * mock delegates to the real matrix by default; individual tests narrow it.
 */

const { canMock } = vi.hoisted(() => ({ canMock: vi.fn() }));

vi.mock('@/shared/auth/roles', async () => {
  const actual = await vi.importActual<typeof import('@/shared/auth/roles')>(
    '@/shared/auth/roles',
  );
  return { ...actual, can: canMock };
});

function useRealMatrix() {
  // Default: delegate to the real RBAC matrix so today's behavior is exercised.
  return import('@/shared/auth/roles').then(({ PERMISSIONS }) => {
    canMock.mockImplementation((role: Role | null | undefined, action: Action) => {
      if (!role) return false;
      return PERMISSIONS[action].includes(role);
    });
  });
}

function renderSettingsAs(role: Role, route = '/settings/workspace') {
  const repository = new FakePanelRepository(buildPanelState());
  repository.currentUser = {
    id: 1,
    email: 'user@reelradar.test',
    role,
    orgId: 1,
    org: { id: 1, name: 'Test Co', logo: null, description: null },
  };
  renderWithProviders(<SettingsPage />, {
    repository,
    route,
    path: '/settings/:tab',
  });
  return repository;
}

const tabButton = (name: RegExp) => screen.findByRole('button', { name });

describe('Settings RBAC tab visibility', () => {
  afterEach(() => {
    canMock.mockReset();
  });

  test('owner sees Workspace, Team, Integrations and Billing tabs', async () => {
    await useRealMatrix();
    renderSettingsAs('owner');
    expect(await tabButton(/Workspace/)).toBeInTheDocument();
    expect(await tabButton(/Team/)).toBeInTheDocument();
    expect(await tabButton(/Integrations/)).toBeInTheDocument();
    expect(await tabButton(/Billing/)).toBeInTheDocument();
  });

  test('admin sees Workspace, Team, Integrations and Billing tabs', async () => {
    await useRealMatrix();
    renderSettingsAs('admin');
    expect(await tabButton(/Workspace/)).toBeInTheDocument();
    expect(await tabButton(/Team/)).toBeInTheDocument();
    expect(await tabButton(/Integrations/)).toBeInTheDocument();
    expect(await tabButton(/Billing/)).toBeInTheDocument();
  });

});

describe('Settings tab visibility is derived from can()', () => {
  afterEach(() => {
    canMock.mockReset();
  });

  test('a matrix that denies view_billing hides the Billing tab', async () => {
    // Billing is gated by view_billing (owner/admin in the real matrix). Denying it
    // must hide the tab while the view_settings-gated tabs remain — proving the rail
    // reads can(view_billing), not a hard-coded owner/admin check.
    canMock.mockImplementation((_role: Role | null | undefined, action: Action) => {
      return action !== 'view_billing';
    });
    renderSettingsAs('owner');

    expect(await tabButton(/Workspace/)).toBeInTheDocument();
    expect(await tabButton(/Integrations/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Billing/ })).not.toBeInTheDocument();
  });

  test('a matrix that denies view_team hides the Team tab', async () => {
    // Allow everything except view_team — proves the UI reads can() rather than
    // hard-coding today's owner/admin matrix.
    canMock.mockImplementation((_role: Role | null | undefined, action: Action) => {
      return action !== 'view_team';
    });
    renderSettingsAs('owner');

    expect(await tabButton(/Workspace/)).toBeInTheDocument();
    expect(await tabButton(/Integrations/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Team/ })).not.toBeInTheDocument();
  });

  test('denying the toggle_integration WRITE action does not hide the Integrations tab', async () => {
    // The Integrations tab is gated by a VIEW action (view_settings), not the write
    // action. Toggling is enforced separately (server-side + inside the panel), so a
    // role that can view settings but not toggle still sees the tab. This test would
    // FAIL if visibility were (incorrectly) tied to toggle_integration.
    canMock.mockImplementation((_role: Role | null | undefined, action: Action) => {
      return action !== 'toggle_integration';
    });
    renderSettingsAs('owner');

    expect(await tabButton(/Workspace/)).toBeInTheDocument();
    expect(await tabButton(/Integrations/)).toBeInTheDocument();
    expect(await tabButton(/Team/)).toBeInTheDocument();
  });

  test('requesting a denied tab falls back to the first permitted panel', async () => {
    // Land directly on /settings/team while view_team is denied: the page must not
    // render TeamPanel and instead fall back to the first permitted tab (Workspace).
    canMock.mockImplementation((_role: Role | null | undefined, action: Action) => {
      return action !== 'view_team';
    });
    renderSettingsAs('owner', '/settings/team');

    // Workspace panel rendered (its "Company" card), Team panel did not ("Members").
    expect(await screen.findByText('Company')).toBeInTheDocument();
    expect(screen.queryByText('Members')).not.toBeInTheDocument();
  });
});
