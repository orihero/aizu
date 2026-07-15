/**
 * Tolerant localStorage helpers. Reads and writes are best-effort and NEVER
 * throw — private-mode browsers, quota errors, and garbage/stale values all
 * resolve to `null` rather than taking the page down. Values are JSON-encoded
 * and validated on the way out so a since-removed enum (e.g. a deleted status
 * filter) can't poison the UI. Mirrors the try/catch pattern in useTheme.tsx.
 */

/**
 * Read and validate a persisted value. Returns `null` when the key is missing,
 * the JSON is malformed, or `validate` rejects the parsed shape.
 */
export function readStored<T>(key: string, validate: (raw: unknown) => T | null): T | null {
  try {
    const raw = localStorage.getItem(key);
    if (raw === null) return null;
    return validate(JSON.parse(raw) as unknown);
  } catch {
    return null;
  }
}

/** Persist a value as JSON. Best-effort — storage failures are swallowed. */
export function writeStored(key: string, value: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // ignore — persistence is best-effort (private mode, quota, etc.)
  }
}
