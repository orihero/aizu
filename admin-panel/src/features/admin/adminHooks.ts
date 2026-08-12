import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { usePanelRepository } from '@/shared/api/repositoryContext';
import { queryKeys } from '@/shared/api/queryKeys';
import { unwrap } from '@/shared/lib/result';
import type {
  AdminOrgLeadsQuery,
  ControlFlagSetInput,
  EnqueueJobInput,
  ExecutionBackend,
  ImpersonateInput,
  MintEnrolmentTokenInput,
} from '@/shared/schemas/admin';

const MODEL_COMPARISON_STATS_POLL_MS = 15_000;

/** Workers heartbeat every ~20s; poll a touch faster so presence stays fresh. */
const FLEET_POLL_MS = 10_000;
/** Default rows fetched for the audit log (server clamps 1..1000). */
const AUDIT_LIMIT = 200;

// ---- reads ----------------------------------------------------------------

export function useFleet() {
  const repository = usePanelRepository();
  return useQuery({
    queryKey: queryKeys.adminFleet,
    queryFn: async () => unwrap(await repository.fetchFleet()),
    refetchInterval: FLEET_POLL_MS,
    staleTime: 2_000,
  });
}

export function useControlFlags() {
  const repository = usePanelRepository();
  return useQuery({
    queryKey: queryKeys.adminControlFlags,
    queryFn: async () => unwrap(await repository.fetchControlFlags()),
    staleTime: 5_000,
  });
}

export function useEnrolmentTokens() {
  const repository = usePanelRepository();
  return useQuery({
    queryKey: queryKeys.adminEnrolmentTokens,
    queryFn: async () => unwrap(await repository.fetchEnrolmentTokens()),
    staleTime: 5_000,
  });
}

export function useAdminOrgs() {
  const repository = usePanelRepository();
  return useQuery({
    queryKey: queryKeys.adminOrgs,
    queryFn: async () => unwrap(await repository.fetchAdminOrgs()),
    staleTime: 10_000,
  });
}

export function useAdminOrgCampaigns(orgId: number) {
  const repository = usePanelRepository();
  return useQuery({
    queryKey: queryKeys.adminOrgCampaigns(orgId),
    queryFn: async () => unwrap(await repository.fetchAdminOrgCampaigns(orgId)),
    staleTime: 10_000,
  });
}

export function useAdminOrgLeads(query: AdminOrgLeadsQuery) {
  const repository = usePanelRepository();
  return useQuery({
    queryKey: queryKeys.adminOrgLeads(query.orgId, query.page, query.pageSize),
    queryFn: async () => unwrap(await repository.fetchAdminOrgLeads(query)),
    placeholderData: keepPreviousData,
    staleTime: 10_000,
  });
}

export function useAudit() {
  const repository = usePanelRepository();
  return useQuery({
    queryKey: queryKeys.adminAudit,
    queryFn: async () => unwrap(await repository.fetchAudit(AUDIT_LIMIT)),
    staleTime: 5_000,
  });
}

export function useExecutionBackend() {
  const repository = usePanelRepository();
  return useQuery({
    queryKey: queryKeys.adminExecutionBackend,
    queryFn: async () => unwrap(await repository.fetchExecutionBackend()),
    staleTime: 10_000,
  });
}

export function useModelComparisonSettings() {
  const repository = usePanelRepository();
  return useQuery({
    queryKey: queryKeys.adminModelComparison,
    queryFn: async () => unwrap(await repository.fetchModelComparisonSettings()),
    staleTime: 10_000,
  });
}

export function useModelComparisonStats() {
  const repository = usePanelRepository();
  return useQuery({
    queryKey: queryKeys.adminModelComparisonStats,
    queryFn: async () => unwrap(await repository.fetchModelComparisonStats()),
    refetchInterval: MODEL_COMPARISON_STATS_POLL_MS,
    staleTime: 5_000,
  });
}

// ---- writes ---------------------------------------------------------------

export function useSetControlFlag() {
  const repository = usePanelRepository();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: ControlFlagSetInput) => {
      unwrap(await repository.setControlFlag(input));
    },
    // A flag change shifts both the flags list and (via halt/drain) the fleet view.
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.adminControlFlags });
      void queryClient.invalidateQueries({ queryKey: queryKeys.adminFleet });
    },
  });
}

export function useRevokeWorker() {
  const repository = usePanelRepository();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (workerId: string) => {
      unwrap(await repository.revokeWorker(workerId));
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.adminFleet });
    },
  });
}

export function useMintEnrolmentToken() {
  const repository = usePanelRepository();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: MintEnrolmentTokenInput) =>
      unwrap(await repository.mintEnrolmentToken(input)),
    // The token isn't redeemed yet — no worker exists from this alone, so the
    // fleet view is untouched; only the enrolment-tokens list grew by one.
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.adminEnrolmentTokens });
    },
  });
}

export function useRevokeEnrolmentToken() {
  const repository = usePanelRepository();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (tokenId: string) => {
      unwrap(await repository.revokeEnrolmentToken(tokenId));
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.adminEnrolmentTokens });
    },
  });
}

export function useEnqueueJob() {
  const repository = usePanelRepository();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: EnqueueJobInput) => unwrap(await repository.enqueueJob(input)),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.adminFleet });
    },
  });
}

export function useSetExecutionBackend() {
  const repository = usePanelRepository();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (backend: ExecutionBackend) => {
      unwrap(await repository.setExecutionBackend(backend));
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.adminExecutionBackend });
    },
  });
}

export function useSetModelComparisonEnabled() {
  const repository = usePanelRepository();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (enabled: boolean) => {
      unwrap(await repository.setModelComparisonEnabled(enabled));
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.adminModelComparison });
    },
  });
}

export function useStartImpersonation() {
  const repository = usePanelRepository();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: ImpersonateInput) => {
      unwrap(await repository.startImpersonation(input));
    },
    // Every cached org read is now the target org's — drop them all so nothing stale lingers.
    onSettled: () => queryClient.invalidateQueries(),
  });
}
