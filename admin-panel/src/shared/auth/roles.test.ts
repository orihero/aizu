import { describe, expect, test } from 'vitest';
import {
  ROLES,
  ASSIGNABLE_ROLES,
  can,
  canAssignRole,
  canManageTarget,
  roleHome,
  type Action,
  type Role,
} from './roles';

// The authoritative matrix — kept identical to engine/tests/test_rbac.py EXPECTED
// so the frontend gate and the server gate never drift.
const EXPECTED: Record<Action, Role[]> = {
  view_dashboard: ['owner', 'admin', 'viewer'],
  view_campaigns: ['owner', 'admin', 'viewer'],
  view_leads: ['owner', 'admin', 'member', 'viewer'],
  view_reports: ['owner', 'admin', 'viewer'],
  view_settings: ['owner', 'admin'],
  view_team: ['owner', 'admin'],
  edit_campaigns: ['owner', 'admin'],
  run_campaigns: ['owner', 'admin'],
  edit_leads: ['owner', 'admin', 'member'],
  bulk_edit_leads: ['owner', 'admin'],
  edit_settings: ['owner', 'admin'],
  toggle_integration: ['owner', 'admin'],
  invite_member: ['owner', 'admin'],
  manage_member: ['owner', 'admin'],
  manage_admin: ['owner'],
  transfer_ownership: ['owner'],
};

describe('can() matrix', () => {
  test('matches the authoritative matrix for every role × action', () => {
    (Object.keys(EXPECTED) as Action[]).forEach((action) => {
      ROLES.forEach((role) => {
        expect(can(role, action)).toBe(EXPECTED[action].includes(role));
      });
    });
  });

  test('fails closed for an unknown/absent role', () => {
    expect(can(null, 'view_leads')).toBe(false);
    expect(can(undefined, 'view_leads')).toBe(false);
  });

  test('member is strictly leads-only', () => {
    (Object.keys(EXPECTED) as Action[]).forEach((action) => {
      const leadsOnly = action === 'view_leads' || action === 'edit_leads';
      expect(can('member', action)).toBe(leadsOnly);
    });
  });

  test('viewer can read but never write or see settings/team', () => {
    expect(can('viewer', 'view_campaigns')).toBe(true);
    expect(can('viewer', 'edit_campaigns')).toBe(false);
    expect(can('viewer', 'edit_leads')).toBe(false);
    expect(can('viewer', 'view_settings')).toBe(false);
    expect(can('viewer', 'view_team')).toBe(false);
  });
});

describe('team-management asymmetry', () => {
  test('owner manages everyone; admin only members/viewers', () => {
    ROLES.forEach((target) => { expect(canManageTarget('owner', target)).toBe(true); });
    expect(canManageTarget('admin', 'member')).toBe(true);
    expect(canManageTarget('admin', 'viewer')).toBe(true);
    expect(canManageTarget('admin', 'admin')).toBe(false);
    expect(canManageTarget('admin', 'owner')).toBe(false);
  });

  test('an admin may ADD an admin but not EDIT one', () => {
    expect(canAssignRole('admin', 'admin')).toBe(true);
    expect(canManageTarget('admin', 'admin')).toBe(false);
  });

  test('owner is never assignable; assignable set excludes it', () => {
    expect(canAssignRole('owner', 'owner')).toBe(false);
    expect(canAssignRole('admin', 'owner')).toBe(false);
    expect(ASSIGNABLE_ROLES).not.toContain('owner');
  });

  test('members and viewers manage nobody', () => {
    (['member', 'viewer'] as Role[]).forEach((actor) =>
      { ROLES.forEach((target) => { expect(canManageTarget(actor, target)).toBe(false); }); },
    );
  });
});

describe('roleHome', () => {
  test('member lands on leads, everyone else on dashboard', () => {
    expect(roleHome('member')).toBe('/leads');
    expect(roleHome('owner')).toBe('/dashboard');
    expect(roleHome('admin')).toBe('/dashboard');
    expect(roleHome('viewer')).toBe('/dashboard');
    expect(roleHome(null)).toBe('/dashboard');
  });
});
