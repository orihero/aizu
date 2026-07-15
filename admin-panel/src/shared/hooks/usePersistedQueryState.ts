import { useCallback } from 'react';
import { useSearchParams } from 'react-router-dom';
import { readStored, writeStored } from '@/shared/lib/storage';

/**
 * A single piece of UI state (a filter, a sort, a period) that survives a page
 * refresh. The URL search param is the source of truth; localStorage is the
 * last-used fallback. Resolution precedence on every read:
 *
 *   URL param (parsed + valid)  →  localStorage (parsed + valid)  →  default
 *
 * Setting the value writes BOTH: the URL (via `setSearchParams`, replacing the
 * history entry so filter churn doesn't spam back/forward) and localStorage.
 * When the value equals its default, the param is dropped to keep URLs clean.
 *
 * Works under the hash router — `useSearchParams` operates on the hash query.
 */
export interface PersistedQueryStateOptions<T> {
  /** URL search param name. */
  readonly paramKey: string;
  /** localStorage key for the last-used fallback. */
  readonly storageKey: string;
  readonly defaultValue: T;
  /** URL string → T. Return `null` for an absent/invalid param. */
  readonly parse: (raw: string) => T | null;
  /** T → URL string. Return `null` to omit the param (value is the default). */
  readonly serialize: (value: T) => string | null;
  /** localStorage shape guard. Return `null` to reject a stale/garbage value. */
  readonly validate: (raw: unknown) => T | null;
}

export function usePersistedQueryState<T>(
  options: PersistedQueryStateOptions<T>,
): readonly [T, (next: T) => void] {
  const { paramKey, storageKey, defaultValue, parse, serialize, validate } = options;
  const [searchParams, setSearchParams] = useSearchParams();

  const rawParam = searchParams.get(paramKey);
  const fromUrl = rawParam === null ? null : parse(rawParam);
  const value = fromUrl ?? readStored(storageKey, validate) ?? defaultValue;

  const setValue = useCallback(
    (next: T) => {
      writeStored(storageKey, next);
      const encoded = serialize(next);
      setSearchParams(
        (prev) => {
          const params = new URLSearchParams(prev);
          if (encoded === null) params.delete(paramKey);
          else params.set(paramKey, encoded);
          return params;
        },
        { replace: true },
      );
    },
    [paramKey, storageKey, serialize, setSearchParams],
  );

  return [value, setValue] as const;
}
