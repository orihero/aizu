import type { QueryClient, QueryKey } from '@tanstack/react-query';
import { queryKeys } from '@/shared/api/queryKeys';
import type { LeadsPayload, Match } from '@/shared/types/domain';

/**
 * Optimistic-update helpers for the paginated leads cache. A lead write (status,
 * note) can't target one campaign-scoped query any more — the leads list is split
 * across many cached pages — so these patch the `items` of EVERY cached leads page at
 * once via `setQueriesData(queryKeys.leads)`, and snapshot them for rollback. The
 * `mapItems` transform is pure (Match[] → Match[]); callers never mutate in place.
 */
export type LeadsSnapshot = [QueryKey, LeadsPayload | undefined][];

export async function patchLeadsPages(
  queryClient: QueryClient,
  mapItems: (items: readonly Match[]) => Match[],
): Promise<LeadsSnapshot> {
  await queryClient.cancelQueries({ queryKey: queryKeys.leads });
  const snapshot = queryClient.getQueriesData<LeadsPayload>({ queryKey: queryKeys.leads });
  queryClient.setQueriesData<LeadsPayload>({ queryKey: queryKeys.leads }, (payload) =>
    payload ? { ...payload, items: mapItems(payload.items) } : payload);
  return snapshot;
}

export function restoreLeadsPages(queryClient: QueryClient, snapshot: LeadsSnapshot): void {
  for (const [key, data] of snapshot) queryClient.setQueryData(key, data);
}

/** Re-sync the lead list + the dashboard pipeline tiles a status/note write shifts. */
export function invalidateLeadSurfaces(queryClient: QueryClient): void {
  void queryClient.invalidateQueries({ queryKey: queryKeys.leads });
  void queryClient.invalidateQueries({ queryKey: queryKeys.dashboard });
}
