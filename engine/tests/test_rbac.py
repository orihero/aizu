"""RBAC policy matrix — exhaustive role × action coverage and the owner/admin
asymmetry (admins may ADD admins but not EDIT them)."""
from aizu import rbac


# The authoritative capability matrix (mirrors the approved plan + roles.ts).
# action -> set of roles that MUST be allowed; every other role MUST be denied.
EXPECTED = {
    "view_dashboard": {"owner", "admin", "viewer"},
    "view_campaigns": {"owner", "admin", "viewer"},
    "view_leads":     {"owner", "admin", "member", "viewer"},
    "view_reports":   {"owner", "admin", "viewer"},
    "view_settings":  {"owner", "admin"},
    "view_team":      {"owner", "admin"},
    "view_billing":   {"owner", "admin"},
    "edit_campaigns":     {"owner", "admin"},
    "run_campaigns":      {"owner", "admin"},
    "edit_leads":         {"owner", "admin", "member"},
    "bulk_edit_leads":    {"owner", "admin"},
    "reveal_lead":        {"owner", "admin", "member"},
    "edit_settings":      {"owner", "admin"},
    "toggle_integration": {"owner", "admin"},
    "manage_billing":     {"owner", "admin"},
    "invite_member":      {"owner", "admin"},
    "manage_member":      {"owner", "admin"},
    "manage_admin":       {"owner"},
    "transfer_ownership": {"owner"},
    "fix_agent":          {"owner", "admin"},
}


def test_permission_matrix_is_exhaustive_and_exact():
    assert set(EXPECTED) == rbac.ACTIONS, "test matrix and rbac.PERMISSIONS drifted"
    for action, allowed in EXPECTED.items():
        for role in rbac.ROLES:
            assert rbac.can(role, action) is (role in allowed), (
                f"{role} {action}: expected {role in allowed}")


def test_unknown_role_or_action_denied():
    assert rbac.can("superuser", "view_leads") is False
    assert rbac.can(None, "view_leads") is False
    assert rbac.can("owner", "nuke_everything") is False  # unknown action fails closed


def test_member_is_strictly_leads_only():
    leads = {"view_leads", "edit_leads", "reveal_lead"}
    for action in rbac.ACTIONS:
        assert rbac.can("member", action) is (action in leads)


def test_viewer_is_read_only():
    for action in rbac.ACTIONS:
        if action.startswith("view_"):
            continue
        assert rbac.can("viewer", action) is False
    # but cannot view settings/team
    assert rbac.can("viewer", "view_settings") is False
    assert rbac.can("viewer", "view_team") is False


def test_can_manage_target_owner_manages_everyone():
    for target in rbac.ROLES:
        assert rbac.can_manage_target("owner", target) is True


def test_admin_manages_only_member_and_viewer():
    assert rbac.can_manage_target("admin", "member") is True
    assert rbac.can_manage_target("admin", "viewer") is True
    assert rbac.can_manage_target("admin", "admin") is False  # owner-only
    assert rbac.can_manage_target("admin", "owner") is False


def test_member_and_viewer_manage_nobody():
    for actor in ("member", "viewer"):
        for target in rbac.ROLES:
            assert rbac.can_manage_target(actor, target) is False


def test_admin_can_add_admin_but_not_edit_admin():
    # the asymmetry the spec calls out
    assert rbac.can_assign_role("admin", "admin") is True
    assert rbac.can_manage_target("admin", "admin") is False


def test_owner_role_is_never_assignable():
    assert rbac.can_assign_role("owner", "owner") is False
    assert rbac.can_assign_role("admin", "owner") is False


def test_assignable_roles_excludes_owner():
    assert "owner" not in rbac.ASSIGNABLE_ROLES
    assert set(rbac.ASSIGNABLE_ROLES) == {"admin", "member", "viewer"}
