import type { AppError } from '@/shared/lib/result';

/** A user-facing description of a run-start (POST /api/run) failure, used by the
 *  RunDrawer's inline error. `agentNotReady` flags the specific 409 gate (the CDP/
 *  Instagram warmed-browser agent isn't up) so the drawer can point the operator at
 *  the fix; `upgrade` flags a plan/billing 402 so it can offer the upgrade link.
 *  Everything else (single-run lock, not-runnable campaign, network) just surfaces
 *  the server's own message. */
export interface RunStartErrorDescription {
  /** The headline. For a 402 this is PLAIN language, not the server's wording. */
  readonly message: string;
  /** The server's own specifics (cap size, reset date), shown under the headline when
   *  we replaced its wording. Null when `message` already IS the server's message. */
  readonly detail: string | null;
  readonly agentNotReady: boolean;
  /** Render the "Upgrade plan" CTA. Only ever true for a billing 402 — never for a
   *  503/missing-AI-key failure, which is an unconfigured provider (see
   *  `describeGenerateError`), not a plan the customer can buy their way out of. */
  readonly upgrade: boolean;
}

const GENERIC = 'Something went wrong starting the run.';

// Plain-language headlines for the two 402s the run gate can answer with. The server's
// copy names real numbers and a reset date — useful, but it opens with "Plan limit
// reached", which reads as a system fault rather than "you are out of leads until the
// 3rd". So the headline is rewritten and the server's sentence is kept as the detail.
const CAP_REACHED = 'You’ve used every lead your plan includes this billing period.';
const SUBSCRIPTION_INACTIVE = 'Your subscription isn’t active, so new runs are paused.';
const PLAN_BLOCKED = 'Your plan doesn’t allow this run right now.';

/** Which 402 this is. The lead-cap message names leads; the inactive one names the
 *  subscription. Anything else falls through to the neutral plan wording rather than
 *  guessing — a wrong specific is worse than a right general. */
function describePaymentRequired(message: string): string {
  if (/\bleads?\b/i.test(message)) return CAP_REACHED;
  if (/subscription/i.test(message)) return SUBSCRIPTION_INACTIVE;
  return PLAN_BLOCKED;
}

/**
 * Pure mapping from a typed AppError to the drawer's inline-error copy.
 *
 * The 402 branch is keyed off the STATUS, never off the message text. Two other
 * failures in this app also say "unavailable" at the customer — AI drafting with no
 * OpenRouter key (503) and the agent-not-ready gate (409) — and neither is fixed by
 * upgrading. Matching on status keeps "buy a bigger plan" attached to the one failure
 * a bigger plan actually fixes.
 */
export function describeRunStartError(error: AppError): RunStartErrorDescription {
  const raw = error.message.trim();
  if (error.status === 402) {
    return {
      message: describePaymentRequired(raw),
      detail: raw || null,
      agentNotReady: false,
      upgrade: true,
    };
  }
  return {
    message: raw || GENERIC,
    detail: null,
    agentNotReady: error.code === 'agent_not_ready',
    upgrade: false,
  };
}
