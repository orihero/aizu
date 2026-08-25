import { useState } from 'react';
import { Check, Copy, Link2, Trash2, UserPlus, Users } from 'lucide-react';
import { Card, CardBody, CardHeader } from '@/shared/ui/Card';
import { Button } from '@/shared/ui/Button';
import { Avatar } from '@/shared/ui/Avatar';
import { EmptyState } from '@/shared/ui/EmptyState';
import { useAuth } from '@/shared/hooks/useAuth';
import {
  useAddTeamMember,
  useCreateInvite,
  useRemoveMember,
  useRevokeInvite,
  useUpdateMemberRole,
} from '@/shared/hooks/useWriteMutations';
import {
  ASSIGNABLE_ROLES,
  ROLE_LABELS,
  canAssignRole,
  canManageTarget,
  type Role,
} from '@/shared/auth/roles';
import type { Invite, TeamMember } from '@/shared/types/domain';
import { Field, INPUT_CLASS } from './SettingsField';

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const MIN_PASSWORD_LENGTH = 8;

/** The roles the current actor may hand out (owner is never assignable here). */
function assignableBy(actorRole: Role | null): Role[] {
  return ASSIGNABLE_ROLES.filter((r) => canAssignRole(actorRole, r));
}

function RoleSelect({
  value,
  options,
  onChange,
  disabled,
  ariaLabel,
}: {
  readonly value: Role;
  readonly options: readonly Role[];
  readonly onChange: (role: Role) => void;
  readonly disabled?: boolean;
  readonly ariaLabel?: string;
}) {
  return (
    <select
      aria-label={ariaLabel}
      className={INPUT_CLASS}
      value={value}
      disabled={disabled}
      onChange={(e) => {
        onChange(e.target.value as Role);
      }}
    >
      {options.map((role) => (
        <option key={role} value={role}>
          {ROLE_LABELS[role]}
        </option>
      ))}
    </select>
  );
}

/** Direct-add: create a real account with a password the inviter sets. */
function DirectAddForm({ assignable }: { readonly assignable: readonly Role[] }) {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [role, setRole] = useState<Role>(assignable[0] ?? 'member');
  const mutation = useAddTeamMember();

  const canAdd =
    EMAIL_RE.test(email.trim()) && password.length >= MIN_PASSWORD_LENGTH && !mutation.isPending;

  const onAdd = () => {
    if (!canAdd) return;
    mutation.mutate(
      { email: email.trim().toLowerCase(), password, role },
      {
        onSuccess: () => {
          setEmail('');
          setPassword('');
        },
      },
    );
  };

  return (
    <div className="flex flex-col gap-3 rounded-tile border border-border bg-surface-2 p-4">
      <div className="grid gap-3 sm:grid-cols-[1.4fr_1fr_auto]">
        <Field label="Email" htmlFor="add-email">
          <input
            id="add-email"
            type="email"
            placeholder="teammate@company.com"
            className={INPUT_CLASS}
            value={email}
            onChange={(e) => {
              setEmail(e.target.value);
            }}
          />
        </Field>
        <Field label="Temporary password" htmlFor="add-password" hint="At least 8 characters.">
          <input
            id="add-password"
            type="text"
            placeholder="Share it with them"
            className={INPUT_CLASS}
            value={password}
            onChange={(e) => {
              setPassword(e.target.value);
            }}
          />
        </Field>
        <Field label="Role" htmlFor="add-role">
          <RoleSelect value={role} options={assignable} onChange={setRole} />
        </Field>
      </div>
      <div className="flex items-center gap-3">
        <Button onClick={onAdd} disabled={!canAdd}>
          <UserPlus className="size-3.5" aria-hidden />
          {mutation.isPending ? 'Adding…' : 'Add member'}
        </Button>
        {mutation.isError ? (
          <span className="text-xs font-medium text-danger">
            {mutation.error instanceof Error ? mutation.error.message : 'Add failed.'}
          </span>
        ) : mutation.isSuccess ? (
          <span className="inline-flex items-center gap-1 text-xs font-medium text-success">
            <Check className="size-3.5" aria-hidden />
            Member added
          </span>
        ) : null}
      </div>
    </div>
  );
}

/** Invite link: generate a shareable URL the teammate opens to set their own password. */
function InviteLinkForm({ assignable }: { readonly assignable: readonly Role[] }) {
  const [role, setRole] = useState<Role>(assignable[0] ?? 'member');
  const [link, setLink] = useState<string>();
  const [copied, setCopied] = useState(false);
  const mutation = useCreateInvite();

  const onCreate = () => {
    if (mutation.isPending) return;
    setCopied(false);
    mutation.mutate(
      { role },
      {
        onSuccess: (data) => {
          // Hash router under /app/: the join URL is origin + '/app/#' + the returned
          // path. The landing's legacy-hash shim would bounce a bare origin + '/#'
          // link too, but an invite is a fresh link — generate it correctly at the
          // source rather than leaning on a shim kept only for old bookmarks.
          setLink(`${window.location.origin}/app/#${data.path}`);
        },
      },
    );
  };

  const onCopy = () => {
    if (!link) return;
    void navigator.clipboard.writeText(link).then(() => {
      setCopied(true);
    });
  };

  return (
    <div className="flex flex-col gap-3 rounded-tile border border-border bg-surface-2 p-4">
      <div className="flex flex-wrap items-end gap-3">
        <Field label="Invite link role" htmlFor="invite-role">
          <RoleSelect value={role} options={assignable} onChange={setRole} />
        </Field>
        <Button variant="ghost" onClick={onCreate} disabled={mutation.isPending}>
          <Link2 className="size-3.5" aria-hidden />
          {mutation.isPending ? 'Generating…' : 'Generate invite link'}
        </Button>
      </div>
      {mutation.isError ? (
        <span className="text-xs font-medium text-danger">
          {mutation.error instanceof Error ? mutation.error.message : 'Could not create link.'}
        </span>
      ) : null}
      {link ? (
        <div className="flex items-center gap-2 rounded-tile border border-border bg-surface px-3 py-2">
          <input
            readOnly
            value={link}
            aria-label="Invite link"
            className="min-w-0 grow bg-transparent text-[12px] text-text-muted outline-none"
            onFocus={(e) => {
              e.currentTarget.select();
            }}
          />
          <Button variant="ghost" onClick={onCopy} aria-label="Copy invite link">
            {copied ? <Check className="size-3.5 text-success" aria-hidden /> : <Copy className="size-3.5" aria-hidden />}
            {copied ? 'Copied' : 'Copy'}
          </Button>
        </div>
      ) : null}
      <p className="text-[11px] text-text-faint">
        The link is shown once and expires in 14 days. Share it directly with your teammate.
      </p>
    </div>
  );
}

function MemberRow({
  member,
  actorRole,
  actorId,
  ownerCount,
}: {
  readonly member: TeamMember;
  readonly actorRole: Role | null;
  readonly actorId: number | null;
  readonly ownerCount: number;
}) {
  const updateRole = useUpdateMemberRole();
  const removeMember = useRemoveMember();

  const isSelf = member.userId !== null && member.userId === actorId;
  const isLastOwner = member.role === 'owner' && ownerCount <= 1;
  const manageable = canManageTarget(actorRole, member.role) && member.userId !== null;
  // Editing a role may only assign roles the actor can MANAGE — so an admin can't
  // promote a member to admin (an admin it then couldn't edit). Mirrors the server.
  const editableRoles = ASSIGNABLE_ROLES.filter((r) => canManageTarget(actorRole, r));
  // The owner role isn't assignable via this control, so an owner row's select is
  // read-only (ownership transfer is a separate, deliberate action).
  const roleOptions = member.role === 'owner' ? (['owner'] as Role[]) : editableRoles;
  const canEditRole = manageable && member.role !== 'owner';
  const canRemove = manageable && !isSelf && !isLastOwner;

  return (
    <div className="flex items-center gap-3 px-5 py-3">
      <Avatar name={member.initials || member.name} size="lg" />
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-semibold text-text">
          {member.name}
          {isSelf ? <span className="ml-2 text-[11px] font-normal text-text-faint">you</span> : null}
        </div>
        <div className="truncate text-xs text-text-muted">{member.email ?? 'No email'}</div>
      </div>
      <div className="w-36 shrink-0">
        <RoleSelect
          ariaLabel={`Role for ${member.name}`}
          value={member.role}
          options={roleOptions}
          disabled={!canEditRole || updateRole.isPending}
          onChange={(role) => {
            if (member.userId !== null) updateRole.mutate({ userId: member.userId, role });
          }}
        />
      </div>
      <Button
        variant="ghost"
        aria-label={`Remove ${member.name}`}
        disabled={!canRemove || removeMember.isPending}
        onClick={() => {
          if (member.userId !== null) removeMember.mutate(member.userId);
        }}
      >
        <Trash2 className="size-3.5" aria-hidden />
      </Button>
    </div>
  );
}

function PendingInviteRow({ invite }: { readonly invite: Invite }) {
  const revoke = useRevokeInvite();
  return (
    <div className="flex items-center gap-3 px-5 py-3">
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-semibold text-text">
          {invite.email || 'Anyone with the link'}
        </div>
        <div className="text-xs text-text-muted">
          Invited as {ROLE_LABELS[invite.role]} · pending
        </div>
      </div>
      <Button
        variant="ghost"
        aria-label="Revoke invite"
        disabled={revoke.isPending}
        onClick={() => {
          revoke.mutate(invite.id);
        }}
      >
        <Trash2 className="size-3.5" aria-hidden />
      </Button>
    </div>
  );
}

export function TeamPanel({
  members,
  invites,
}: {
  readonly members: readonly TeamMember[];
  readonly invites: readonly Invite[];
}) {
  const { user } = useAuth();
  const actorRole = user?.role ?? null;
  const actorId = user?.id ?? null;
  const assignable = assignableBy(actorRole);
  const ownerCount = members.filter((m) => m.role === 'owner').length;
  const canInvite = assignable.length > 0; // owner/admin

  return (
    <div className="flex flex-col gap-5">
      {canInvite ? (
        <>
          <DirectAddForm assignable={assignable} />
          <InviteLinkForm assignable={assignable} />
        </>
      ) : null}

      <Card>
        <CardHeader title="Members" subtitle={`${members.length} in this company`} />
        {members.length === 0 ? (
          <CardBody>
            <EmptyState
              icon={Users}
              title="No members yet"
              description="Add a teammate above to give them access to this company."
            />
          </CardBody>
        ) : (
          <div className="divide-y divide-border">
            {members.map((member) => (
              <MemberRow
                key={member.id}
                member={member}
                actorRole={actorRole}
                actorId={actorId}
                ownerCount={ownerCount}
              />
            ))}
          </div>
        )}
      </Card>

      {invites.length > 0 ? (
        <Card>
          <CardHeader title="Pending invites" subtitle={`${invites.length} awaiting acceptance`} />
          <div className="divide-y divide-border">
            {invites.map((invite) => (
              <PendingInviteRow key={invite.id} invite={invite} />
            ))}
          </div>
        </Card>
      ) : null}
    </div>
  );
}
