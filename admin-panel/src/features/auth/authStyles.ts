/**
 * Shared input styling for the auth forms. Auth fields are larger than the
 * settings controls (this is the hero surface), so they get their own token
 * rather than importing settings' INPUT_CLASS — keeps the two features decoupled.
 */
export const AUTH_INPUT_CLASS =
  'w-full rounded-xl border border-border bg-surface px-3.5 py-2.5 text-sm text-text ' +
  'placeholder:text-text-faint transition focus:border-accent focus:outline-none ' +
  'focus:ring-2 focus:ring-accent/30 disabled:cursor-not-allowed disabled:opacity-50';

/** Applied to an input in its error state. */
export const AUTH_INPUT_ERROR_CLASS =
  'border-danger focus:border-danger focus:ring-danger/30';
