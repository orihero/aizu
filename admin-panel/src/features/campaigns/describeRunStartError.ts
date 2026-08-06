import type { AppError } from '@/shared/lib/result';

/** A user-facing description of a run-start (POST /api/run) failure, used by the
 *  RunDrawer's inline error. `agentNotReady` flags the specific 409 gate (the CDP/
 *  Instagram warmed-browser agent isn't up) so the drawer can point the operator at
 *  the fix — everything else (single-run lock, not-runnable campaign, network) just
 *  surfaces the server's own message. */
export interface RunStartErrorDescription {
  readonly message: string;
  readonly agentNotReady: boolean;
}

const GENERIC = 'Something went wrong starting the run.';

/** Pure mapping from a typed AppError to the drawer's inline-error copy. */
export function describeRunStartError(error: AppError): RunStartErrorDescription {
  return {
    message: error.message.trim() || GENERIC,
    agentNotReady: error.code === 'agent_not_ready',
  };
}
