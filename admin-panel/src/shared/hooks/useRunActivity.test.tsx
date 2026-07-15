import { describe, expect, test } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { AppProviders } from '@/app/providers';
import { FakePanelRepository } from '@/test/fakePanelRepository';
import { buildPanelState, buildRunActivity, buildRunEvent } from '@/test/fixtures';
import { useRunActivity } from './useRunActivity';

function wrapperFor(repository: FakePanelRepository) {
  return function Wrapper({ children }: { readonly children: ReactNode }) {
    return <AppProviders repository={repository}>{children}</AppProviders>;
  };
}

describe('useRunActivity', () => {
  test('loads the first page and exposes accumulated events + counters', async () => {
    const repo = new FakePanelRepository(buildPanelState());
    repo.runActivity = buildRunActivity({
      events: [buildRunEvent({ id: 1 }), buildRunEvent({ id: 2 })],
      cursor: 2,
    });

    const { result } = renderHook(() => useRunActivity('run-001'), { wrapper: wrapperFor(repo) });

    await waitFor(() => { expect(result.current.activity).not.toBeNull(); });
    expect(result.current.activity?.events.map((e) => e.id)).toEqual([1, 2]);
    expect(result.current.activity?.counters?.matches).toBe(3);
    expect(repo.runActivityFetches[0]).toEqual({ runId: 'run-001', afterSeq: 0 });
  });

  test('does not poll when no run is active (runId null)', async () => {
    const repo = new FakePanelRepository(buildPanelState());
    repo.runActivity = buildRunActivity();

    const { result } = renderHook(() => useRunActivity(null), { wrapper: wrapperFor(repo) });

    // Give any erroneous fetch a chance to fire, then assert none did.
    await new Promise((r) => setTimeout(r, 20));
    expect(repo.runActivityFetches).toHaveLength(0);
    expect(result.current.activity).toBeNull();
  });

  test('surfaces an error for an unknown / cross-org run', async () => {
    const repo = new FakePanelRepository(buildPanelState());
    repo.runActivity = null; // fake → http 404

    const { result } = renderHook(() => useRunActivity('ghost'), { wrapper: wrapperFor(repo) });

    await waitFor(() => { expect(result.current.isError).toBe(true); });
    expect(result.current.activity).toBeNull();
  });
});
