import { useMutation, useQueryClient, type QueryKey } from '@tanstack/react-query';
import { usePanelRepository } from '@/shared/api/repositoryContext';
import { queryKeys } from '@/shared/api/queryKeys';
import { unwrap } from '@/shared/lib/result';
import {
  invalidateLeadSurfaces,
  patchLeadsPages,
  restoreLeadsPages,
  type LeadsSnapshot,
} from '@/shared/hooks/leadsCache';
import type {
  ArchiveCampaignInput,
  BulkStatusRequest,
  CampaignInput,
  DirectAddInput,
  ScheduleCampaignInput,
  GenerateCampaignInput,
  Integration,
  InterviewRequest,
  InviteCreateInput,
  Match,
  OrganizationInput,
  RedditConnectInput,
  RunInput,
  TelegramVerifyInput,
  UpdateRoleInput,
  WorkspaceSettingsInput,
} from '@/shared/types/domain';

/**
 * Panel write mutations. Every write re-syncs the single `panelState` query on
 * settle — the engine DB stays the source of truth. The bulk status write also
 * applies an optimistic patch (rolled back on error) so large selections feel
 * instant; the other writes just invalidate.
 */

function useInvalidate() {
  const queryClient = useQueryClient();
  // Invalidate each affected per-page query so it re-syncs from the server.
  return (...keys: readonly QueryKey[]) => {
    for (const key of keys) {
      void queryClient.invalidateQueries({ queryKey: key });
    }
  };
}

/** Pure item transform: flip status for every selected lead across a page of matches. */
function withBulkStatus(items: readonly Match[], request: BulkStatusRequest): Match[] {
  const ids = new Set(request.items.map((i) => i.commentId));
  return items.map((m) =>
    ids.has(m.commentId) && m.campaignId === request.campaignId
      ? { ...m, status: request.status }
      : m,
  );
}

export function useBulkSetStatus() {
  const repository = usePanelRepository();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (request: BulkStatusRequest) => {
      unwrap(await repository.bulkSetStatus(request));
    },
    onMutate: async (request): Promise<{ snapshot: LeadsSnapshot }> => {
      const snapshot = await patchLeadsPages(queryClient, (items) => withBulkStatus(items, request));
      return { snapshot };
    },
    onError: (_error, _request, context) => {
      if (context) restoreLeadsPages(queryClient, context.snapshot);
    },
    // Status changes shift lead lists + the dashboard pipeline tiles.
    onSettled: () => { invalidateLeadSurfaces(queryClient); },
  });
}

export function useCreateCampaign() {
  const repository = usePanelRepository();
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async (input: CampaignInput) => {
      unwrap(await repository.createCampaign(input));
    },
    onSettled: () => { invalidate(queryKeys.campaigns, queryKeys.dashboard); },
  });
}

/** Archive or un-archive a campaign. Invalidates campaigns + dashboard (archive
 * changes the active-campaign count and may have stopped a run). */
export function useArchiveCampaign() {
  const repository = usePanelRepository();
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async (input: ArchiveCampaignInput) => {
      unwrap(await repository.archiveCampaign(input));
    },
    onSettled: () => { invalidate(queryKeys.campaigns, queryKeys.dashboard); },
  });
}

/** Arm or clear a campaign's recurring schedule. Invalidates campaigns (the card
 * shows the schedule badge + next-run) and dashboard. */
export function useScheduleCampaign() {
  const repository = usePanelRepository();
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async (input: ScheduleCampaignInput) => {
      unwrap(await repository.scheduleCampaign(input));
    },
    onSettled: () => { invalidate(queryKeys.campaigns, queryKeys.dashboard); },
  });
}

/** Draft a campaign with AI from a description / URL / screenshot. Not
 * invalidating — generation writes nothing server-side (the user reviews and
 * saves the returned prefill through the normal create path). */
export function useGenerateCampaign() {
  const repository = usePanelRepository();
  return useMutation({
    mutationFn: async (input: GenerateCampaignInput) =>
      unwrap(await repository.generateCampaign(input)),
  });
}

/** One round of the conversational campaign interview. Like generate, it writes
 *  nothing server-side — the wizard threads the reply into the next round and,
 *  on done, into the final generate call. */
export function useRunInterview() {
  const repository = usePanelRepository();
  return useMutation({
    mutationFn: async (input: InterviewRequest) =>
      unwrap(await repository.runInterview(input)),
  });
}

export function useRunCampaign() {
  const repository = usePanelRepository();
  const invalidate = useInvalidate();
  return useMutation({
    // Returns the run-start result so the drawer can poll a fleet run's live feed by
    // its runId (in-process runs surface via the RUN block instead → runId null).
    mutationFn: async (input: RunInput) => unwrap(await repository.runCampaign(input)),
    // Re-sync so the polled RUN block reflects the freshly accepted run.
    onSettled: () => { invalidate(queryKeys.campaigns, queryKeys.dashboard); },
  });
}

export function useStopRun() {
  const repository = usePanelRepository();
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async () => {
      unwrap(await repository.stopRun());
    },
    // Re-sync so the RUN block drops the run it just aborted.
    onSettled: () => { invalidate(queryKeys.campaigns, queryKeys.dashboard); },
  });
}

export function usePauseRun() {
  const repository = usePanelRepository();
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async () => {
      unwrap(await repository.pauseRun());
    },
    // Re-sync so the RUN block reflects the new paused state in the card/drawer.
    onSettled: () => { invalidate(queryKeys.campaigns, queryKeys.dashboard); },
  });
}

export function useResumeRun() {
  const repository = usePanelRepository();
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async () => {
      unwrap(await repository.resumeRun());
    },
    onSettled: () => { invalidate(queryKeys.campaigns, queryKeys.dashboard); },
  });
}

export function useUpdateSettings() {
  const repository = usePanelRepository();
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async (input: WorkspaceSettingsInput) => {
      unwrap(await repository.updateSettings(input));
    },
    onSettled: () => { invalidate(queryKeys.settings); },
  });
}

export function useUpdateOrganization() {
  const repository = usePanelRepository();
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async (input: OrganizationInput) => {
      unwrap(await repository.updateOrganization(input));
    },
    onSettled: () => { invalidate(queryKeys.settings); },
  });
}

export function useAddTeamMember() {
  const repository = usePanelRepository();
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async (input: DirectAddInput) => {
      unwrap(await repository.addTeamMember(input));
    },
    onSettled: () => { invalidate(queryKeys.settings); },
  });
}

export function useCreateInvite() {
  const repository = usePanelRepository();
  const invalidate = useInvalidate();
  return useMutation({
    // Returns the shareable invite link for the caller to copy.
    mutationFn: async (input: InviteCreateInput) => unwrap(await repository.createInvite(input)),
    onSettled: () => { invalidate(queryKeys.settings); },
  });
}

export function useRevokeInvite() {
  const repository = usePanelRepository();
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async (id: string) => {
      unwrap(await repository.revokeInvite(id));
    },
    onSettled: () => { invalidate(queryKeys.settings); },
  });
}

export function useUpdateMemberRole() {
  const repository = usePanelRepository();
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async (input: UpdateRoleInput) => {
      unwrap(await repository.updateMemberRole(input));
    },
    onSettled: () => { invalidate(queryKeys.settings); },
  });
}

export function useRemoveMember() {
  const repository = usePanelRepository();
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async (userId: number) => {
      unwrap(await repository.removeMember(userId));
    },
    onSettled: () => { invalidate(queryKeys.settings); },
  });
}

export function useToggleIntegration() {
  const repository = usePanelRepository();
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async ({ integration, connected }: { integration: Integration; connected: boolean }) => {
      unwrap(await repository.toggleIntegration(integration, connected));
    },
    onSettled: () => { invalidate(queryKeys.settings); },
  });
}

export function useConnectYouTube() {
  const repository = usePanelRepository();
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async (apiKey: string) => {
      unwrap(await repository.connectYouTube(apiKey));
    },
    onSettled: () => { invalidate(queryKeys.settings); },
  });
}

export function useConnectReddit() {
  const repository = usePanelRepository();
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: async (input: RedditConnectInput) => {
      unwrap(await repository.connectReddit(input));
    },
    onSettled: () => { invalidate(queryKeys.settings); },
  });
}

/** Step 1 of the Telegram wizard: send a login code; returns the wizard token.
 * Not invalidating — nothing has changed server-side until verify succeeds. */
export function useStartTelegramLogin() {
  const repository = usePanelRepository();
  return useMutation({
    mutationFn: async (phone: string) => unwrap(await repository.startTelegramLogin(phone)),
  });
}

/** Step 2 of the Telegram wizard: submit the code (+ 2FA password) → connected. */
export function useVerifyTelegramLogin() {
  const repository = usePanelRepository();
  const invalidate = useInvalidate();
  return useMutation({
    // Returns the verify result ({ needsPassword }) so the wizard can branch to
    // the 2FA step on success.
    mutationFn: async (input: TelegramVerifyInput) =>
      unwrap(await repository.verifyTelegramLogin(input)),
    onSettled: () => { invalidate(queryKeys.settings); },
  });
}
