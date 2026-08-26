"""Role-based access control policy — the single source of truth for "who can do
what" in a multi-tenant org (PRD: multi-tenancy + RBAC).

This module is pure (stdlib only, no I/O) so both the HTTP gate (`server.py`) and
the panel-state builder (`panel.py`) can import it, and it is mirrored 1:1 by the
frontend at `admin-panel/src/shared/auth/roles.ts` — keep the two in lockstep.

The four roles are NOT a linear rank: a `member` can edit leads (which a `viewer`
cannot) yet cannot view campaigns/reports (which a `viewer` can). So permission is
an explicit action → allowed-roles matrix, not a `>=` comparison.

Capability summary (see the approved plan):
  - owner  : everything; the only role that may edit/remove admins or transfer ownership.
  - admin  : campaigns + leads + team + settings; may invite anyone (incl. admins) but
             may NOT edit/remove existing admins.
  - member : leads only (view + edit + reveal), nothing else.
  - viewer : read-only — dashboard, campaigns, leads, reports. No writes, no settings,
             and no lead reveal (it sees the anonymized list, never the raw identity).
"""
from __future__ import annotations

OWNER = "owner"
ADMIN = "admin"
MEMBER = "member"
VIEWER = "viewer"

# Order = display/seniority order only (NOT an authority comparison — see module doc).
ROLES: tuple[str, ...] = (OWNER, ADMIN, MEMBER, VIEWER)

# Roles a brand-new account can be assigned. `owner` is established at signup or via
# an explicit ownership transfer — never handed out by invite/direct-add.
ASSIGNABLE_ROLES: tuple[str, ...] = (ADMIN, MEMBER, VIEWER)

# action → the set of roles allowed to perform it. Reads start `view_`, writes don't.
PERMISSIONS: dict[str, frozenset[str]] = {
    # --- reads (page visibility) ---
    "view_dashboard": frozenset({OWNER, ADMIN, VIEWER}),
    "view_campaigns": frozenset({OWNER, ADMIN, VIEWER}),
    "view_leads":     frozenset({OWNER, ADMIN, MEMBER, VIEWER}),
    "view_reports":   frozenset({OWNER, ADMIN, VIEWER}),
    "view_settings":  frozenset({OWNER, ADMIN}),
    "view_team":      frozenset({OWNER, ADMIN}),
    "view_billing":   frozenset({OWNER, ADMIN}),  # billing tab + BILLING on /api/settings
    # --- writes ---
    "edit_campaigns":     frozenset({OWNER, ADMIN}),
    "run_campaigns":      frozenset({OWNER, ADMIN}),
    "edit_leads":         frozenset({OWNER, ADMIN, MEMBER}),  # single-lead status change + notes
    "bulk_edit_leads":    frozenset({OWNER, ADMIN}),           # bulk status change (incl. archive) — owner/admin only
    # v27 reveal-on-demand: un-redact ONE lead's HANDLE, and nothing else about the
    # person (POST /api/lead/reveal). Every lead is anonymized by default; this is the
    # explicit, audited un-redaction — and its scope is the handle ALONE. The comment
    # body and the post it sits on are superadmin-only with no org-facing route at all,
    # so this permission is not the gate on them: there is no gate, there is no route.
    # It is a WRITE-shaped permission even though the request itself only reads, because
    # what it grants is disclosure, not access to a page. `viewer` is deliberately
    # excluded: it is the read-only role, and the anonymized list is what it may read.
    "reveal_lead":        frozenset({OWNER, ADMIN, MEMBER}),
    "edit_settings":      frozenset({OWNER, ADMIN}),
    "toggle_integration": frozenset({OWNER, ADMIN}),
    "manage_billing":     frozenset({OWNER, ADMIN}),  # start checkout / open billing portal
    "fix_agent":          frozenset({OWNER, ADMIN}),  # launch Chrome / open the login tab
    "invite_member":      frozenset({OWNER, ADMIN}),
    "manage_member":      frozenset({OWNER, ADMIN}),  # edit/remove a member or viewer
    "manage_admin":       frozenset({OWNER}),         # edit/remove an admin — owner only
    "transfer_ownership": frozenset({OWNER}),
}

ACTIONS: frozenset[str] = frozenset(PERMISSIONS)


def is_valid_role(role: object) -> bool:
    return isinstance(role, str) and role in ROLES


def can(role: str | None, action: str) -> bool:
    """True iff `role` is allowed to perform `action`. Unknown role/action → False
    (fail closed). `action` MUST be a known action — a typo'd action denies, never
    silently allows."""
    if action not in PERMISSIONS:
        return False
    return role in PERMISSIONS[action]


def can_manage_target(actor_role: str | None, target_role: str | None) -> bool:
    """Whether `actor_role` may EDIT or REMOVE an existing user whose role is
    `target_role`. Owners manage everyone; admins manage only members/viewers;
    nobody else manages anyone. (Creating/inviting an admin is a separate check —
    `can_assign_role` — because admins may *add* admins but not *edit* them.)"""
    if not is_valid_role(actor_role) or not is_valid_role(target_role):
        return False
    if actor_role == OWNER:
        return True
    if actor_role == ADMIN:
        return target_role in (MEMBER, VIEWER)
    return False


def can_assign_role(actor_role: str | None, target_role: str | None) -> bool:
    """Whether `actor_role` may assign `target_role` when inviting or direct-adding a
    new user. Per spec, "owners AND admins can add everyone" — so an admin may create
    an admin (but, per `can_manage_target`, cannot later edit that admin). `owner` is
    never assignable here."""
    if target_role not in ASSIGNABLE_ROLES:
        return False
    if actor_role == OWNER:
        return True
    if actor_role == ADMIN:
        return target_role in ASSIGNABLE_ROLES
    return False
