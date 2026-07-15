import { useState } from 'react';
import { UserCog } from 'lucide-react';
import { useAuth } from '@/shared/hooks/useAuth';
import { usePanelRepository } from '@/shared/api/repositoryContext';

/**
 * Org-side impersonation banner (the cross-plane hand-off, Gap F). When a superadmin has
 * opened this workspace under an active impersonation, /api/auth/me reports
 * `impersonated: true` — this makes it unmistakable which tenant is being viewed AS, and
 * "Exit" ends the impersonation and returns to the admin console. A full-page navigation
 * (not client routing) is used so BOTH planes re-bootstrap cleanly against the cookies.
 */
export function ImpersonationBanner() {
  const { user } = useAuth();
  const repository = usePanelRepository();
  const [exiting, setExiting] = useState(false);

  if (!user?.impersonated) return null;

  const orgName = user.org?.name ?? (user.orgId !== null ? `org #${user.orgId}` : 'a tenant');

  async function onExit() {
    setExiting(true);
    // Best-effort end; then hard-navigate back to the admin console regardless so the
    // operator is never stranded in an org they can no longer act in.
    await repository.endImpersonation();
    window.location.assign('/admin/orgs');
  }

  return (
    <div
      role="alert"
      className="mb-5 flex flex-wrap items-center gap-3 rounded-tile bg-danger-soft px-4 py-3 text-danger"
    >
      <UserCog className="size-5 shrink-0" aria-hidden />
      <div className="grow text-[13px] font-semibold">
        Viewing <span className="font-bold">{orgName}</span> as a platform admin
        <span className="font-normal"> — actions you take are recorded against this tenant.</span>
      </div>
      <button
        type="button"
        onClick={() => { void onExit(); }}
        disabled={exiting}
        className="inline-flex items-center gap-1.5 rounded-full bg-danger px-3.5 py-1.5 text-xs font-bold text-white transition hover:shadow-lift active:scale-95 disabled:opacity-50"
      >
        {exiting ? 'Exiting…' : 'Exit impersonation'}
      </button>
    </div>
  );
}
