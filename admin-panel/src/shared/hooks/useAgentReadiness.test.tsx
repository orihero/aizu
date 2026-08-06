import { describe, expect, test } from 'vitest';
import { renderHook, waitFor, act } from '@testing-library/react';
import type { ReactNode } from 'react';
import { AppProviders } from '@/app/providers';
import { FakePanelRepository } from '@/test/fakePanelRepository';
import { buildAgentReadiness, buildPanelState } from '@/test/fixtures';
import { useAgentReadiness } from './useAgentReadiness';

function wrapperFor(repository: FakePanelRepository) {
  return function Wrapper({ children }: { readonly children: ReactNode }) {
    return <AppProviders repository={repository}>{children}</AppProviders>;
  };
}

describe('useAgentReadiness', () => {
  test('loads readiness with a plain (non-refresh) fetch', async () => {
    const repo = new FakePanelRepository(buildPanelState());
    repo.agentReadiness = buildAgentReadiness({ ready: true });

    const { result } = renderHook(() => useAgentReadiness(), { wrapper: wrapperFor(repo) });

    await waitFor(() => { expect(result.current.readiness).toBeDefined(); });
    expect(result.current.readiness?.ready).toBe(true);
    expect(repo.agentReadinessFetches[0]).toEqual({});
  });

  test('recheck() forces a refresh:true fetch and toggles isRechecking', async () => {
    const repo = new FakePanelRepository(buildPanelState());
    repo.agentReadiness = buildAgentReadiness({ ready: false, cdp: 'unreachable' });

    const { result } = renderHook(() => useAgentReadiness(), { wrapper: wrapperFor(repo) });
    await waitFor(() => { expect(result.current.readiness).toBeDefined(); });

    await act(async () => {
      await result.current.recheck();
    });

    expect(repo.agentReadinessFetches.at(-1)).toEqual({ refresh: true });
    expect(result.current.isRechecking).toBe(false);
  });
});
