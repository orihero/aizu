import { useCallback, useEffect, useMemo, useReducer, useRef } from 'react';
import { useSearchParams } from 'react-router-dom';
import {
  DEFAULT_LEAD_SORT,
  EMPTY_LEAD_FILTERS,
  LEAD_STATUS_ORDER,
  type LeadFilters,
  type LeadSort,
  type LeadSortKey,
} from '@/shared/selectors/leads';
import { readStored, writeStored } from '@/shared/lib/storage';
import type { MatchStatus } from '@/shared/types/domain';

/**
 * Local UI state for the Leads page: the filter set, sort, current page, and
 * the set of selected commentIds. Any filter change resets the page to 1 so the
 * operator never lands on an out-of-range page after narrowing the list.
 *
 * The refresh-surviving slice — `{ filters, sort, page }` — is derived purely
 * from the URL search params (the single source of truth); localStorage seeds
 * those params once on mount when the URL arrives bare (the "last used"
 * fallback). `selected` stays ephemeral: it holds commentIds that may not exist
 * after a refresh. See usePersistedQueryState for the single-value pages that
 * share this precedence model (URL → storage → default).
 */
export interface LeadUiState {
  readonly filters: LeadFilters;
  readonly sort: LeadSort;
  readonly page: number;
  readonly selected: ReadonlySet<string>;
}

/** The refresh-surviving part of the Leads UI state. */
interface PersistedSlice {
  readonly filters: LeadFilters;
  readonly sort: LeadSort;
  readonly page: number;
}

const STORAGE_KEY = 'leads:filters';
const PARAM = {
  query: 'q',
  status: 'status',
  platform: 'platform',
  campaign: 'campaign',
  sort: 'sort',
  dir: 'dir',
  page: 'page',
} as const;
const PARAM_KEYS: readonly string[] = Object.values(PARAM);

const DEFAULT_SLICE: PersistedSlice = {
  filters: EMPTY_LEAD_FILTERS,
  sort: DEFAULT_LEAD_SORT,
  page: 1,
};

// v27: `username` is no longer a sort key (an org-facing lead has no handle), so a
// stored/bookmarked `sort=username` now fails `isSortKey` and falls back to the
// default — the same path any other stale value takes.
const LEAD_SORT_KEYS: readonly LeadSortKey[] = [
  'intent',
  'platform',
  'status',
  'score',
  'captured',
];

function isStatus(value: unknown): value is MatchStatus | 'all' {
  return value === 'all' || LEAD_STATUS_ORDER.includes(value as MatchStatus);
}

function isSortKey(value: unknown): value is LeadSortKey {
  return LEAD_SORT_KEYS.includes(value as LeadSortKey);
}

function isDir(value: unknown): value is LeadSort['dir'] {
  return value === 'asc' || value === 'desc';
}

function parsePage(value: unknown): number | null {
  const n = typeof value === 'number' ? value : Number.parseInt(String(value), 10);
  return Number.isInteger(n) && n >= 1 ? n : null;
}

/** Shape guard for the localStorage fallback — rejects any stale/garbage value. */
function validateSlice(raw: unknown): PersistedSlice | null {
  if (typeof raw !== 'object' || raw === null) return null;
  const o = raw as Record<string, unknown>;
  const f = o.filters as Record<string, unknown> | undefined;
  const s = o.sort as Record<string, unknown> | undefined;
  if (!f || !s) return null;
  if (typeof f.query !== 'string' || typeof f.platform !== 'string' || !isStatus(f.status)) {
    return null;
  }
  if (typeof f.campaign !== 'string') return null;
  if (!isSortKey(s.key) || !isDir(s.dir)) return null;
  const page = parsePage(o.page);
  if (page === null) return null;
  return {
    filters: { query: f.query, status: f.status, platform: f.platform, campaign: f.campaign },
    sort: { key: s.key, dir: s.dir },
    page,
  };
}

/** The slice the URL alone implies (absent/invalid params fall back to default). */
function deriveSlice(params: URLSearchParams): PersistedSlice {
  const query = params.get(PARAM.query);
  const status = params.get(PARAM.status);
  const platform = params.get(PARAM.platform);
  const campaign = params.get(PARAM.campaign);
  const sortKey = params.get(PARAM.sort);
  const dir = params.get(PARAM.dir);
  const page = params.get(PARAM.page);
  return {
    filters: {
      query: query ?? DEFAULT_SLICE.filters.query,
      status: isStatus(status) ? status : DEFAULT_SLICE.filters.status,
      platform: platform ?? DEFAULT_SLICE.filters.platform,
      campaign: campaign ?? DEFAULT_SLICE.filters.campaign,
    },
    sort: {
      key: isSortKey(sortKey) ? sortKey : DEFAULT_SLICE.sort.key,
      dir: isDir(dir) ? dir : DEFAULT_SLICE.sort.dir,
    },
    page: parsePage(page) ?? DEFAULT_SLICE.page,
  };
}

/** Apply a slice to a params object, omitting any field that's at its default. */
function writeSliceParams(params: URLSearchParams, slice: PersistedSlice): URLSearchParams {
  const desired: Record<string, string | null> = {
    [PARAM.query]: slice.filters.query === '' ? null : slice.filters.query,
    [PARAM.status]: slice.filters.status === 'all' ? null : slice.filters.status,
    [PARAM.platform]: slice.filters.platform === 'all' ? null : slice.filters.platform,
    [PARAM.campaign]: slice.filters.campaign === 'all' ? null : slice.filters.campaign,
    [PARAM.sort]: slice.sort.key === DEFAULT_LEAD_SORT.key ? null : slice.sort.key,
    [PARAM.dir]: slice.sort.dir === DEFAULT_LEAD_SORT.dir ? null : slice.sort.dir,
    [PARAM.page]: slice.page <= 1 ? null : String(slice.page),
  };
  for (const [key, val] of Object.entries(desired)) {
    if (val === null) params.delete(key);
    else params.set(key, val);
  }
  return params;
}

/**
 * Clicking the active column flips its direction; clicking a new column selects
 * it with a sensible default — descending for numeric/time columns (highest /
 * newest first), ascending for text columns (A→Z).
 */
function nextSort(current: LeadSort, key: LeadSortKey): LeadSort {
  if (current.key === key) {
    return { key, dir: current.dir === 'asc' ? 'desc' : 'asc' };
  }
  const dir = key === 'score' || key === 'captured' ? 'desc' : 'asc';
  return { key, dir };
}

// --- Selection (ephemeral; never persisted) ---------------------------------

type SelectAction =
  | { type: 'toggle'; id: string }
  | { type: 'selectAll'; ids: readonly string[] }
  | { type: 'clear' };

function withoutIds(selected: ReadonlySet<string>, ids: readonly string[]): Set<string> {
  const next = new Set(selected);
  for (const id of ids) next.delete(id);
  return next;
}

function selectionReducer(selected: ReadonlySet<string>, action: SelectAction): ReadonlySet<string> {
  switch (action.type) {
    case 'toggle': {
      const next = new Set(selected);
      if (next.has(action.id)) next.delete(action.id);
      else next.add(action.id);
      return next;
    }
    case 'selectAll': {
      // Header checkbox toggles the current page: deselect when all are already
      // selected, otherwise add the page's ids to the existing selection.
      const allSelected = action.ids.every((id) => selected.has(id));
      return allSelected
        ? withoutIds(selected, action.ids)
        : new Set([...selected, ...action.ids]);
    }
    case 'clear':
      return new Set<string>();
  }
}

export function useLeadFilters() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [selected, dispatchSelection] = useReducer(selectionReducer, undefined, () => new Set<string>());

  // State is a pure function of the URL — no reducer mirror to drift out of
  // sync, so back/forward "just works" and no effect can fight another.
  const search = searchParams.toString();
  const slice = useMemo(() => deriveSlice(searchParams), [search]); // eslint-disable-line react-hooks/exhaustive-deps
  const sliceRef = useRef(slice);
  sliceRef.current = slice;

  const commit = useCallback(
    (next: PersistedSlice) => {
      setSearchParams((prev) => writeSliceParams(new URLSearchParams(prev), next), { replace: true });
    },
    [setSearchParams],
  );

  // Seed the URL from the last-used localStorage slice, but only on first mount
  // and only when the URL arrived with none of our params (a clean entry).
  const seededRef = useRef(false);
  useEffect(() => {
    if (seededRef.current) return;
    seededRef.current = true;
    const bare = PARAM_KEYS.every((key) => searchParams.get(key) === null);
    if (!bare) return;
    const stored = readStored(STORAGE_KEY, validateSlice);
    if (stored) commit(stored);
  }, [searchParams, commit]);

  // Mirror the live slice into localStorage as the "last used" fallback.
  useEffect(() => {
    writeStored(STORAGE_KEY, slice);
  }, [slice]);

  const actions = useMemo(
    () => ({
      setQuery: (query: string) => {
        const s = sliceRef.current;
        commit({ ...s, filters: { ...s.filters, query }, page: 1 });
      },
      setStatus: (status: MatchStatus | 'all') => {
        const s = sliceRef.current;
        commit({ ...s, filters: { ...s.filters, status }, page: 1 });
      },
      setPlatform: (platform: string) => {
        const s = sliceRef.current;
        commit({ ...s, filters: { ...s.filters, platform }, page: 1 });
      },
      setCampaign: (campaign: string) => {
        const s = sliceRef.current;
        commit({ ...s, filters: { ...s.filters, campaign }, page: 1 });
      },
      toggleSort: (key: LeadSortKey) => {
        // Resort from the top — the operator's mental model is "show me the
        // ranked list", not "re-rank whatever page I happen to be on".
        const s = sliceRef.current;
        commit({ ...s, sort: nextSort(s.sort, key), page: 1 });
      },
      setPage: (page: number) => {
        commit({ ...sliceRef.current, page });
      },
      toggleSelect: (id: string) => { dispatchSelection({ type: 'toggle', id }); },
      selectAll: (ids: readonly string[]) => { dispatchSelection({ type: 'selectAll', ids }); },
      clearSelection: () => { dispatchSelection({ type: 'clear' }); },
    }),
    [commit],
  );

  return { ...slice, selected, ...actions };
}
