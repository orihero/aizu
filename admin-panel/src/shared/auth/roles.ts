/**
 * Role-based access control policy — the frontend mirror of the engine's
 * `engine/aizu/rbac.py`. Keep the two in lockstep: the matrices MUST match.
 * The server is the real gate; this drives UX (hiding/disabling controls) only.
 *
 * The four roles are NOT a linear rank: a `member` can edit and reveal leads (a
 * `viewer` cannot) yet cannot view campaigns/reports (a `viewer` can). Permission is an
 * explicit action → allowed-roles matrix, never a `>=` comparison.
 */

export const ROLES = ['owner', 'admin', 'member', 'viewer'] as const
export type Role = (typeof ROLES)[number]

/** Roles assignable to a brand-new account (owner is signup/transfer only). */
export const ASSIGNABLE_ROLES: readonly Role[] = ['admin', 'member', 'viewer']

export type Action =
  | 'view_dashboard'
  | 'view_campaigns'
  | 'view_leads'
  | 'view_reports'
  | 'view_settings'
  | 'view_team'
  | 'view_billing'
  | 'edit_campaigns'
  | 'run_campaigns'
  | 'edit_leads'
  | 'bulk_edit_leads'
  | 'reveal_lead'
  | 'edit_settings'
  | 'toggle_integration'
  | 'manage_billing'
  | 'invite_member'
  | 'manage_member'
  | 'manage_admin'
  | 'transfer_ownership'
  | 'fix_agent'

/** action → roles allowed to perform it (mirror of rbac.PERMISSIONS). */
export const PERMISSIONS: Record<Action, readonly Role[]> = {
  view_dashboard: ['owner', 'admin', 'viewer'],
  view_campaigns: ['owner', 'admin', 'viewer'],
  view_leads: ['owner', 'admin', 'member', 'viewer'],
  view_reports: ['owner', 'admin', 'viewer'],
  view_settings: ['owner', 'admin'],
  view_team: ['owner', 'admin'],
  view_billing: ['owner', 'admin'],
  edit_campaigns: ['owner', 'admin'],
  run_campaigns: ['owner', 'admin'],
  edit_leads: ['owner', 'admin', 'member'],
  bulk_edit_leads: ['owner', 'admin'],
  // v27 reveal-on-demand: un-redact ONE lead's HANDLE, and nothing else about the
  // person (POST /api/lead/reveal). Every lead is anonymized by default; this is the
  // explicit, audited un-redaction — and its scope is the handle ALONE. The comment
  // body and the post it sits on are superadmin-only with no org-facing route at all,
  // so this permission is not the gate on them: there is no gate, there is no route.
  // It is a WRITE-shaped permission even though the request itself only reads, because
  // what it grants is disclosure, not access to a page. `viewer` is deliberately
  // excluded: it is the read-only role, and the anonymized list is what it may read.
  reveal_lead: ['owner', 'admin', 'member'],
  edit_settings: ['owner', 'admin'],
  toggle_integration: ['owner', 'admin'],
  manage_billing: ['owner', 'admin'],
  invite_member: ['owner', 'admin'],
  manage_member: ['owner', 'admin'],
  manage_admin: ['owner'],
  transfer_ownership: ['owner'],
  // Launch/relaunch the warmed Chrome login for the Instagram harvest agent, and the
  // actionable variant of the "agent not ready" banner — owner/admin only (mirrors the
  // team-management writes: a member/viewer only ever sees the informational banner).
  fix_agent: ['owner', 'admin'],
}

export function isValidRole(role: unknown): role is Role {
  return typeof role === 'string' && (ROLES as readonly string[]).includes(role)
}

/** True iff `role` may perform `action`. Unknown role/action → false (fail closed). */
export function can(role: Role | null | undefined, action: Action): boolean {
  if (!role) return false
  return PERMISSIONS[action].includes(role)
}

/** Whether `actorRole` may EDIT or REMOVE an existing user with `targetRole`. */
export function canManageTarget(
  actorRole: Role | null | undefined,
  targetRole: Role | null | undefined,
): boolean {
  if (!isValidRole(actorRole) || !isValidRole(targetRole)) return false
  if (actorRole === 'owner') return true
  if (actorRole === 'admin') return targetRole === 'member' || targetRole === 'viewer'
  return false
}

/** Whether `actorRole` may assign `targetRole` when inviting/adding a new user. */
export function canAssignRole(
  actorRole: Role | null | undefined,
  targetRole: Role | null | undefined,
): boolean {
  if (!targetRole || !(ASSIGNABLE_ROLES as readonly string[]).includes(targetRole)) return false
  if (actorRole === 'owner') return true
  if (actorRole === 'admin') return (ASSIGNABLE_ROLES as readonly string[]).includes(targetRole)
  return false
}

/** Landing route for a role after login (member can only reach Leads). */
export function roleHome(role: Role | null | undefined): string {
  return role === 'member' ? '/leads' : '/dashboard'
}

/** Human label for a role (UI display). */
export const ROLE_LABELS: Record<Role, string> = {
  owner: 'Owner',
  admin: 'Admin',
  member: 'Member',
  viewer: 'Viewer',
}
