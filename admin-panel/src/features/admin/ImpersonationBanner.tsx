import { useState } from 'react';
import { UserCog } from 'lucide-react';
import { usePanelRepository } from '@/shared/api/repositoryContext';
import { useAdminAuth } from './useAdminAuth';

/**
 * Sticky warning shown across the admin subtree while an impersonation is active.
 * Every org-plane read the console makes is served as the target org while this is
 * up, so it must be impossible to miss — and one click ends it.
 */
export function ImpersonationBanner() {
  const { session, refresh } = useAdminAuth();
  const repository = usePanelRepository();
  const [ending, setEnding] = useState(false);

  if (!session?.impersonating) return null;

  const target = session.effectiveUserId !== null
    ? `user #${session.effectiveUserId}`
    : session.effectiveOrgId !== null
      ? `org #${session.effectiveOrgId}`
      : 'a tenant';

  async function onEnd() {
    setEnding(true);
    await repository.endImpersonation();
    await refresh();
    setEnding(false);
  }

  return (
    <div
      role="alert"
      className="mb-5 flex flex-wrap items-center gap-3 rounded-tile bg-danger-soft px-4 py-3 text-danger"
    >
      <UserCog className="size-5 shrink-0" aria-hidden />
      <div className="grow text-[13px] font-semibold">
        Impersonating {target}
        {session.impersonationReason ? (
          <span className="font-normal"> — {session.impersonationReason}</span>
        ) : null}
      </div>
      {/* Hand-off into the org app: a full-page nav so the org AuthProvider bootstraps
          fresh against the admin cookie and picks up the impersonated identity. */}
      <button
        type="button"
        onClick={() => { window.location.assign('/'); }}
        disabled={ending}
        className="inline-flex items-center gap-1.5 rounded-full border border-danger px-3.5 py-1.5 text-xs font-bold text-danger transition hover:bg-danger/10 active:scale-95 disabled:opacity-50"
      >
        Open workspace
      </button>
      <button
        type="button"
        onClick={() => {
          void onEnd();
        }}
        disabled={ending}
        className="inline-flex items-center gap-1.5 rounded-full bg-danger px-3.5 py-1.5 text-xs font-bold text-white transition hover:shadow-lift active:scale-95 disabled:opacity-50"
      >
        {ending ? 'Ending…' : 'End impersonation'}
      </button>
    </div>
  );
}
