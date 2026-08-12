import { useState } from 'react';
import { Loader2, RefreshCw, ShieldAlert } from 'lucide-react';
import { useAgentReadiness } from '@/shared/hooks/useAgentReadiness';
import { useLaunchAgentLogin } from '@/shared/hooks/useWriteMutations';
import { useCan } from '@/shared/hooks/useCan';
import type { AgentReadiness } from '@/shared/types/domain';

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : 'Something went wrong.';
}

/** Name the exact problem from cdp/instagram, falling back to the server's own
 * `detail` for anything the two known states don't already explain. */
function describeAgentProblem(readiness: AgentReadiness): string {
  // On the distributed backend there is no browser on this server at all — the
  // cdp/instagram fields carry worker-fleet presence, so naming Chrome here would
  // point an admin at the wrong machine. The server's `detail` is the true sentence.
  if (readiness.backend === 'distributed') {
    return readiness.detail ?? 'no worker is online to run this';
  }
  if (readiness.cdp !== 'ok') return 'Chrome (CDP) unreachable';
  if (readiness.instagram !== 'logged_in') return 'Instagram session logged out';
  return readiness.detail ?? 'The Instagram agent is not ready';
}

/**
 * Global "agent not ready" banner — the same CDP/Instagram gate a live run needs to
 * pass (POST /api/run's 409 agent_not_ready). Rendered in AppLayout so it's visible
 * wherever campaigns/runs are operated, not just the Campaigns page. Polls readiness
 * on a shared 60s interval (useAgentReadiness); renders nothing once ready.
 *
 * A member/viewer (no `fix_agent`) has no control that would help, so they only get
 * told an admin needs to fix it. An owner/admin gets the specific problem plus the
 * two recovery actions: launch a login browser, then re-check.
 */
export function AgentReadinessBanner() {
  const { readiness, recheck, isRechecking } = useAgentReadiness();
  const canFix = useCan('fix_agent');
  const launchLogin = useLaunchAgentLogin();
  const [launchFeedback, setLaunchFeedback] = useState<string | null>(null);

  if (!readiness || readiness.ready) return null;

  // Nothing to launch on a control plane that holds no browser: in distributed mode
  // the warmed Chrome lives on the worker PC and is signed in from that box.
  const isDistributed = readiness.backend === 'distributed';

  const onLaunch = async () => {
    setLaunchFeedback(null);
    try {
      const result = await launchLogin.mutateAsync();
      setLaunchFeedback(
        result.launched
          ? 'Login browser launched — sign in there, then Re-check.'
          : 'No browser was launched (one may already be open) — check it, then Re-check.',
      );
    } catch {
      // Surfaced below via launchLogin.isError; nothing further to do here.
    }
  };

  return (
    <div
      role="alert"
      className="mb-4 flex flex-wrap items-center gap-3 rounded-tile border border-warn/40 bg-warn-soft px-5 py-3 text-[13px] shadow-tile"
    >
      <ShieldAlert className="size-[18px] shrink-0 text-warn" aria-hidden />

      {canFix ? (
        <>
          <span className="grow">
            <strong className="font-bold text-warn">
              {isDistributed ? 'Agent not ready' : 'Instagram agent not ready'}
            </strong>
            {' — '}
            {describeAgentProblem(readiness)}. Live runs can’t start until this is fixed.
          </span>
          {isDistributed ? null : (
            <button
              type="button"
              onClick={() => { void onLaunch(); }}
              disabled={launchLogin.isPending}
              className="inline-flex shrink-0 items-center gap-1.5 rounded-full border border-warn/40 bg-surface px-3 py-1.5 text-xs font-semibold text-warn transition-colors hover:bg-warn hover:text-white disabled:opacity-50"
            >
              {launchLogin.isPending ? <Loader2 className="size-3.5 animate-spin" aria-hidden /> : null}
              Launch login browser
            </button>
          )}
          <button
            type="button"
            onClick={() => { void recheck(); }}
            disabled={isRechecking}
            className="inline-flex shrink-0 items-center gap-1.5 rounded-full border border-warn/40 bg-surface px-3 py-1.5 text-xs font-semibold text-warn transition-colors hover:bg-warn hover:text-white disabled:opacity-50"
          >
            {isRechecking
              ? <Loader2 className="size-3.5 animate-spin" aria-hidden />
              : <RefreshCw className="size-3.5" aria-hidden />}
            Re-check
          </button>
          {launchFeedback ? (
            <p className="w-full text-xs font-medium text-text-faint">{launchFeedback}</p>
          ) : null}
          {launchLogin.isError ? (
            <p className="w-full text-xs font-medium text-danger">
              Couldn’t launch the login browser: {errorMessage(launchLogin.error)}
            </p>
          ) : null}
        </>
      ) : (
        <span className="grow">
          <strong className="font-bold text-warn">Instagram agent not working</strong>
          {' — '}
          there’s a connection/login problem. An administrator needs to fix this before live runs can start.
        </span>
      )}
    </div>
  );
}
