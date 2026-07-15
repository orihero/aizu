import type { AppError } from '@/shared/lib/result';

/** A user-facing description of a generate failure, used by the composer's error
 *  panel. `canFillManually` drives whether we offer a "Fill it in manually"
 *  escape hatch (only when AI drafting itself is unavailable, not when the input
 *  was the problem). */
export interface GenerateErrorDescription {
  readonly title: string;
  readonly hint: string;
  readonly canFillManually: boolean;
}

const NETWORK: GenerateErrorDescription = {
  title: "Couldn't reach the server. Try again.",
  hint: 'Check your connection and retry.',
  canFillManually: false,
};

const NO_AI_KEY: GenerateErrorDescription = {
  title: 'AI drafting is unavailable (no AI key). You can fill it in manually.',
  hint: 'Ask your administrator to configure an AI provider key, or start from scratch.',
  canFillManually: true,
};

const UNBUILDABLE: GenerateErrorDescription = {
  title: "We couldn't turn that into a draft. Add a clearer description or screenshot.",
  hint: 'A short product description plus a landing-page link works best.',
  canFillManually: false,
};

const GENERIC_TITLE = 'Generation failed.';

/** Whether the error signals the AI provider is unconfigured (no key). */
function isNoAiKey(error: AppError): boolean {
  return error.status === 503 || /openrouter/i.test(error.message);
}

/** Whether the error signals the input couldn't be turned into a draft. */
function isUnbuildable(error: AppError): boolean {
  return error.status === 422 || error.kind === 'validation';
}

/**
 * Pure mapping from a typed AppError to user-facing copy for the composer.
 * Branches the AI-key and unbuildable cases out of the generic http/unknown
 * fall-through so each gets the right copy and manual-fill affordance.
 */
export function describeGenerateError(error: AppError): GenerateErrorDescription {
  if (error.kind === 'network') return NETWORK;
  if (isNoAiKey(error)) return NO_AI_KEY;
  if (isUnbuildable(error)) return UNBUILDABLE;
  // Otherwise surface the server's own message when it has one; never expose a
  // blank panel.
  return {
    title: error.message.trim() || GENERIC_TITLE,
    hint: 'Try again, or adjust your inputs.',
    canFillManually: false,
  };
}
