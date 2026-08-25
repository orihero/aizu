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
  test('exposes the polled progress scalars', async () => {
    const repo = new FakePanelRepository(buildPanelState());
    repo.runActivity = buildRunActivity({
      phase: 'qualifying',
      leadsFound: 7,
      leadsDelivered: 7,
      delivery: 'delivered',
      itemsScanned: 40,
      targetLeads: 10,
    });

    const { result } = renderHook(() => useRunActivity('run-001'), { wrapper: wrapperFor(repo) });

    await waitFor(() => { expect(result.current.activity).not.toBeNull(); });
    expect(result.current.activity?.phase).toBe('qualifying');
    expect(result.current.activity?.leadsFound).toBe(7);
    expect(result.current.activity?.targetLeads).toBe(10);
    expect(repo.runActivityFetches[0]).toEqual({ runId: 'run-001', afterSeq: 0 });
  });

  test('never pages events into the customer app', async () => {
    // v27/B3: even if a bridge sent rows, the hook's projection drops them — the
    // customer app has no path to a run event, filtered or otherwise.
    const repo = new FakePanelRepository(buildPanelState());
    repo.runActivity = buildRunActivity({ events: [buildRunEvent({ id: 1 })] });

    const { result } = renderHook(() => useRunActivity('run-001'), { wrapper: wrapperFor(repo) });

    await waitFor(() => { expect(result.current.activity).not.toBeNull(); });
    expect(result.current.activity).not.toHaveProperty('events');
  });

  test('always polls from cursor 0 — there is nothing to page', async () => {
    const repo = new FakePanelRepository(buildPanelState());
    repo.runActivity = buildRunActivity({ cursor: 99 });

    const { result } = renderHook(() => useRunActivity('run-001'), { wrapper: wrapperFor(repo) });

    await waitFor(() => { expect(result.current.activity).not.toBeNull(); });
    // Even a bridge echoing a non-zero cursor must not make the next poll skip ahead:
    // each page is a whole snapshot, so `after` stays a constant no-op.
    expect(repo.runActivityFetches.every((f) => f.afterSeq === 0)).toBe(true);
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
