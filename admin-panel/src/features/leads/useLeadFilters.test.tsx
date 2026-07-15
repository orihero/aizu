import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it } from 'vitest';
import { act, renderHook, waitFor } from '@testing-library/react';
import { MemoryRouter, useNavigate, useSearchParams } from 'react-router-dom';
import { DEFAULT_LEAD_SORT } from '@/shared/selectors/leads';
import { useLeadFilters } from './useLeadFilters';

const STORAGE_KEY = 'leads:filters';

function wrapper(entry: string) {
  return ({ children }: { children: ReactNode }) => (
    <MemoryRouter initialEntries={[entry]}>{children}</MemoryRouter>
  );
}

function renderLeads(entry: string) {
  return renderHook(
    () => {
      const filters = useLeadFilters();
      const [params] = useSearchParams();
      const navigate = useNavigate();
      return { filters, params, navigate };
    },
    { wrapper: wrapper(entry) },
  );
}

function storedSlice(): Record<string, unknown> | null {
  const raw = localStorage.getItem(STORAGE_KEY);
  return raw ? (JSON.parse(raw) as Record<string, unknown>) : null;
}

describe('useLeadFilters persistence', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it('hydrates the full slice from URL params', () => {
    const { result } = renderLeads(
      '/leads?status=interested&platform=instagram&sort=score&dir=asc&page=3&q=foo',
    );
    const { filters } = result.current;
    expect(filters.filters).toEqual({
      query: 'foo', status: 'interested', platform: 'instagram', campaign: 'all',
    });
    expect(filters.sort).toEqual({ key: 'score', dir: 'asc' });
    expect(filters.page).toBe(3);
  });

  it('falls back to the stored slice when the URL is bare', () => {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        filters: { query: '', status: 'closed', platform: 'all', campaign: 'all' },
        sort: DEFAULT_LEAD_SORT,
        page: 2,
      }),
    );
    const { result } = renderLeads('/leads');
    expect(result.current.filters.filters.status).toBe('closed');
    expect(result.current.filters.page).toBe(2);
  });

  it('ignores a garbage URL value and uses the default', () => {
    const { result } = renderLeads('/leads?status=zzz&sort=bogus');
    expect(result.current.filters.filters.status).toBe('all');
    expect(result.current.filters.sort).toEqual(DEFAULT_LEAD_SORT);
  });

  it('resets the page to 1 and updates the URL when a filter changes', async () => {
    const { result } = renderLeads('/leads?page=3');
    expect(result.current.filters.page).toBe(3);

    act(() => { result.current.filters.setStatus('new'); });

    await waitFor(() => {
      expect(result.current.filters.page).toBe(1);
      expect(result.current.params.get('status')).toBe('new');
      expect(result.current.params.get('page')).toBeNull();
    });
  });

  it('round-trips the page number to the URL and localStorage', async () => {
    const { result } = renderLeads('/leads');
    act(() => { result.current.filters.setPage(2); });

    await waitFor(() => {
      expect(result.current.params.get('page')).toBe('2');
    });
    expect(storedSlice()?.page).toBe(2);
  });

  it('re-seeds state when the URL changes (back/forward)', async () => {
    const { result } = renderLeads('/leads');
    expect(result.current.filters.filters.status).toBe('all');

    act(() => { void result.current.navigate('/leads?status=new'); });

    await waitFor(() => {
      expect(result.current.filters.filters.status).toBe('new');
    });
  });

  it('writes query and platform filters to the URL', async () => {
    const { result } = renderLeads('/leads');
    act(() => { result.current.filters.setQuery('jane'); });
    await waitFor(() => { expect(result.current.params.get('q')).toBe('jane'); });
    act(() => { result.current.filters.setPlatform('youtube'); });
    await waitFor(() => { expect(result.current.params.get('platform')).toBe('youtube'); });
    expect(result.current.filters.filters).toEqual({
      query: 'jane',
      status: 'all',
      platform: 'youtube',
      campaign: 'all',
    });
  });

  it('toggles sort direction on the active column and picks a default on a new one', async () => {
    const { result } = renderLeads('/leads');

    // A new text column sorts ascending; the default (captured) is omitted.
    act(() => { result.current.filters.toggleSort('username'); });
    await waitFor(() => {
      expect(result.current.filters.sort).toEqual({ key: 'username', dir: 'asc' });
      expect(result.current.params.get('sort')).toBe('username');
      expect(result.current.params.get('dir')).toBe('asc');
    });

    // Clicking the active column flips its direction.
    act(() => { result.current.filters.toggleSort('username'); });
    await waitFor(() => {
      expect(result.current.filters.sort).toEqual({ key: 'username', dir: 'desc' });
    });
  });

  it('selects and clears a page of rows without touching the URL', () => {
    const { result } = renderLeads('/leads');
    const ids = ['a', 'b', 'c'];

    act(() => { result.current.filters.selectAll(ids); });
    expect([...result.current.filters.selected].sort()).toEqual(ids);

    // Re-selecting an already-fully-selected page deselects it.
    act(() => { result.current.filters.selectAll(ids); });
    expect(result.current.filters.selected.size).toBe(0);

    act(() => { result.current.filters.selectAll(ids); });
    act(() => { result.current.filters.clearSelection(); });
    expect(result.current.filters.selected.size).toBe(0);
    expect(result.current.params.toString()).toBe('');
  });

  it('does not persist the lead selection', async () => {
    const { result } = renderLeads('/leads');
    act(() => { result.current.filters.toggleSelect('comment-123'); });

    expect(result.current.filters.selected.has('comment-123')).toBe(true);
    // The persisted slice never carries selection (stale ids after a refresh).
    await waitFor(() => { expect(storedSlice()).not.toBeNull(); });
    expect(storedSlice()).not.toHaveProperty('selected');
    expect(localStorage.getItem(STORAGE_KEY)).not.toContain('comment-123');
    expect(result.current.params.toString()).not.toContain('comment-123');
  });
});
