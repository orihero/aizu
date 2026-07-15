import { describe, expect, test } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { AppProviders } from '@/app/providers';
import { FakePanelRepository } from '@/test/fakePanelRepository';
import { buildIntegration, buildPanelState } from '@/test/fixtures';
import {
  useAddTeamMember,
  useBulkSetStatus,
  useArchiveCampaign,
  useCreateCampaign,
  usePauseRun,
  useResumeRun,
  useScheduleCampaign,
  useCreateInvite,
  useRunCampaign,
  useStopRun,
  useToggleIntegration,
} from './useWriteMutations';

function wrapperFor(repository: FakePanelRepository) {
  return function Wrapper({ children }: { readonly children: ReactNode }) {
    return <AppProviders repository={repository}>{children}</AppProviders>;
  };
}

describe('useBulkSetStatus', () => {
  test('records a bulk write with all selected items', async () => {
    const repo = new FakePanelRepository(buildPanelState());
    const { result } = renderHook(() => useBulkSetStatus(), { wrapper: wrapperFor(repo) });

    result.current.mutate({
      campaignId: 'cmp-001',
      status: 'interested',
      items: [{ commentId: 'c1', platform: 'instagram' }],
    });

    await waitFor(() => { expect(repo.bulkWrites).toHaveLength(1); });
    expect(repo.bulkWrites[0]?.status).toBe('interested');
  });
});

describe('useCreateCampaign', () => {
  test('records the campaign input', async () => {
    const repo = new FakePanelRepository(buildPanelState());
    const { result } = renderHook(() => useCreateCampaign(), { wrapper: wrapperFor(repo) });

    result.current.mutate({ campaignId: 'new-x', displayName: 'New X', status: 'draft' });

    await waitFor(() => { expect(repo.campaignCreates).toHaveLength(1); });
    expect(repo.campaignCreates[0]?.campaignId).toBe('new-x');
  });
});

describe('usePauseRun / useResumeRun', () => {
  test('pause records a pause request', async () => {
    const repo = new FakePanelRepository(buildPanelState());
    const { result } = renderHook(() => usePauseRun(), { wrapper: wrapperFor(repo) });
    result.current.mutate();
    await waitFor(() => { expect(repo.pauseRequests).toBe(1); });
  });

  test('resume records a resume request', async () => {
    const repo = new FakePanelRepository(buildPanelState());
    const { result } = renderHook(() => useResumeRun(), { wrapper: wrapperFor(repo) });
    result.current.mutate();
    await waitFor(() => { expect(repo.resumeRequests).toBe(1); });
  });
});

describe('useArchiveCampaign', () => {
  test('records the archive request', async () => {
    const repo = new FakePanelRepository(buildPanelState());
    const { result } = renderHook(() => useArchiveCampaign(), { wrapper: wrapperFor(repo) });

    result.current.mutate({ campaignId: 'cmp-1', archived: true });

    await waitFor(() => { expect(repo.archiveRequests).toHaveLength(1); });
    expect(repo.archiveRequests[0]).toEqual({ campaignId: 'cmp-1', archived: true });
  });
});

describe('useScheduleCampaign', () => {
  test('records the schedule request', async () => {
    const repo = new FakePanelRepository(buildPanelState());
    const { result } = renderHook(() => useScheduleCampaign(), { wrapper: wrapperFor(repo) });

    result.current.mutate({ campaignId: 'cmp-1', enabled: true, kind: 'daily', hour: 9, minute: 0 });

    await waitFor(() => { expect(repo.scheduleRequests).toHaveLength(1); });
    expect(repo.scheduleRequests[0]?.kind).toBe('daily');
  });
});

describe('useRunCampaign', () => {
  test('records the run request with its lead target and safety cap', async () => {
    const repo = new FakePanelRepository(buildPanelState());
    const { result } = renderHook(() => useRunCampaign(), { wrapper: wrapperFor(repo) });

    result.current.mutate({
      campaignId: 'cmp-001', mode: 'live', targetLeadCount: 50, durationMinutes: 120,
    });

    await waitFor(() => { expect(repo.runRequests).toHaveLength(1); });
    expect(repo.runRequests[0]).toEqual({
      campaignId: 'cmp-001', mode: 'live', targetLeadCount: 50, durationMinutes: 120,
    });
  });
});

describe('useStopRun', () => {
  test('records a stop request', async () => {
    const repo = new FakePanelRepository(buildPanelState());
    const { result } = renderHook(() => useStopRun(), { wrapper: wrapperFor(repo) });

    result.current.mutate();

    await waitFor(() => { expect(repo.stopRequests).toBe(1); });
  });

  test('surfaces a failed stop as isError', async () => {
    const repo = new FakePanelRepository(buildPanelState());
    repo.failNextWrite = true;
    const { result } = renderHook(() => useStopRun(), { wrapper: wrapperFor(repo) });

    result.current.mutate();

    await waitFor(() => { expect(result.current.isError).toBe(true); });
    expect(repo.stopRequests).toBe(0);
  });
});

describe('useAddTeamMember', () => {
  test('records a direct-add op', async () => {
    const repo = new FakePanelRepository(buildPanelState());
    const { result } = renderHook(() => useAddTeamMember(), { wrapper: wrapperFor(repo) });

    result.current.mutate({ email: 'sam@acme.com', password: 'longenough1', role: 'member' });

    await waitFor(() => { expect(repo.teamOps).toHaveLength(1); });
    expect(repo.teamOps[0]?.op).toBe('add');
  });
});

describe('useCreateInvite', () => {
  test('returns a shareable invite link', async () => {
    const repo = new FakePanelRepository(buildPanelState());
    const { result } = renderHook(() => useCreateInvite(), { wrapper: wrapperFor(repo) });

    result.current.mutate({ role: 'admin' });

    await waitFor(() => { expect(repo.teamOps).toHaveLength(1); });
    expect(repo.teamOps[0]?.op).toBe('invite');
    expect(result.current.data?.role).toBe('admin');
  });
});

describe('useToggleIntegration', () => {
  test('records the toggle target and state', async () => {
    const repo = new FakePanelRepository(buildPanelState());
    const integration = buildIntegration();
    const { result } = renderHook(() => useToggleIntegration(), { wrapper: wrapperFor(repo) });

    result.current.mutate({ integration, connected: false });

    await waitFor(() => { expect(repo.integrationToggles).toHaveLength(1); });
    expect(repo.integrationToggles[0]).toEqual({ id: integration.id, connected: false });
  });
});
