/**
 * Never-throw boundary type. Every external interaction (HTTP, parsing)
 * returns a Result so failures are values the caller must handle,
 * never exceptions that escape a call site.
 */
export type Result<T, E = AppError> =
  | { readonly ok: true; readonly value: T }
  | { readonly ok: false; readonly error: E };

export interface AppError {
  readonly kind: 'network' | 'http' | 'validation' | 'unknown';
  readonly message: string;
  /** HTTP status when kind === 'http'. */
  readonly status?: number;
  /** Optional machine-readable discriminator for a specific server error shape a
   * caller needs to branch on precisely (e.g. 'agent_not_ready') rather than
   * guessing from `status`/`message` — orthogonal to `kind`, set only by the call
   * sites that parse a response carrying its own error code. */
  readonly code?: string;
}

export const ok = <T>(value: T): Result<T, never> => ({ ok: true, value });

export const err = <E>(error: E): Result<never, E> => ({ ok: false, error });

export const appError = (
  kind: AppError['kind'],
  message: string,
  status?: number,
  code?: string,
): AppError => ({
  kind,
  message,
  ...(status === undefined ? {} : { status }),
  ...(code === undefined ? {} : { code }),
});

/**
 * Error thrown by `unwrap`. Carries the original AppError so throwing contexts
 * (react-query) can still inspect the kind/status — e.g. detect a 401 and drop
 * the session — instead of seeing a status-less generic Error.
 */
export class ResultError extends Error {
  readonly appError: AppError;
  constructor(error: AppError) {
    super(error.message);
    this.name = 'ResultError';
    this.appError = error;
  }
}

/** Unwraps a Result or throws — for contexts (react-query) that expect throwing. */
export function unwrap<T>(result: Result<T>): T {
  if (result.ok) return result.value;
  throw new ResultError(result.error);
}
