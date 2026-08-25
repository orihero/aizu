import type { AppError } from '@/shared/lib/result';

/**
 * User-facing copy for a failed `POST /api/lead/reveal` (Section F).
 *
 * The reveal has exactly one refusal a customer can act on — the period reveal
 * allowance is spent (402) — and it must not read like a bug. Every other failure
 * (403 from the role gate, 404 from the ownership gate, transport) surfaces the
 * server's own message, because none of them is fixed by buying a bigger plan.
 */
export interface RevealErrorDescription {
  /** The headline. For the 402 this is PLAIN language, not the server's wording. */
  readonly message: string;
  /** The server's specifics (cap size, reset date), shown under the headline when we
   *  replaced its wording. Null when `message` already IS the server's message. */
  readonly detail: string | null;
  /** Render the "Upgrade plan" link. Only ever true for the billing 402. */
  readonly upgrade: boolean;
}

const GENERIC = 'The reveal didn’t work.';

/**
 * The plan-limit headline. The server opens with "Plan limit reached", which reads as
 * a system fault rather than "you've opened every lead your plan lets you open this
 * month" — so the headline is rewritten and the server's sentence kept as the detail.
 *
 * It names DISTINCT leads on purpose: the cap counts leads revealed this period, not
 * reveal calls, so re-opening a lead already revealed this period never spends any
 * allowance and can never produce this message. Copy that said "reveals" would tell a
 * customer that re-reading a lead costs them something, which is false.
 */
const CAP_REACHED =
  'You’ve revealed every lead your plan includes this billing period.';

/**
 * Pure mapping from a typed AppError to the drawer's reveal-error copy.
 *
 * Keyed off the STATUS, never the message text — same rule as `describeRunStartError`.
 * A 403 here ("your role does not permit this action") is the viewer gate and a 404 is
 * the ownership gate; neither is a plan the customer can upgrade out of, and offering
 * them an upgrade link would send them to a checkout that fixes nothing.
 */
export function describeRevealError(error: AppError): RevealErrorDescription {
  const raw = error.message.trim();
  if (error.status === 402) {
    return { message: CAP_REACHED, detail: raw || null, upgrade: true };
  }
  return { message: raw || GENERIC, detail: null, upgrade: false };
}
