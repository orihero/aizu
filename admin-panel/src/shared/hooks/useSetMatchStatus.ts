import { useMutation, useQueryClient } from '@tanstack/react-query';
import { usePanelRepository } from '@/shared/api/repositoryContext';
import { unwrap } from '@/shared/lib/result';
import {
  invalidateLeadSurfaces,
  patchLeadsPages,
  restoreLeadsPages,
  type LeadsSnapshot,
} from '@/shared/hooks/leadsCache';
import type { Match, StatusWriteRequest } from '@/shared/types/domain';

/** Pure item transform: flip the matching lead's status across a page of matches. */
function withMatchStatus(items: readonly Match[], request: StatusWriteRequest): Match[] {
  return items.map((m) =>
    m.commentId === request.commentId &&
    m.campaignId === request.campaignId &&
    (request.platform === undefined || m.platform === request.platform)
      ? { ...m, status: request.status }
      : m,
  );
}

/**
 * Status-mark mutation (PRD §11 v1). Optimistically updates every cached leads page,
 * rolls back on failure, and re-syncs from the server either way — the DB stays the
 * single source of truth.
 */
export function useSetMatchStatus() {
  const repository = usePanelRepository();
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: async (request: StatusWriteRequest) =>
      { unwrap(await repository.setMatchStatus(request)); },
    onMutate: async (request): Promise<{ snapshot: LeadsSnapshot }> => {
      const snapshot = await patchLeadsPages(queryClient, (items) => withMatchStatus(items, request));
      return { snapshot };
    },
    onError: (_error, _request, context) => {
      if (context) restoreLeadsPages(queryClient, context.snapshot);
    },
    onSettled: () => { invalidateLeadSurfaces(queryClient); },
  });
}
