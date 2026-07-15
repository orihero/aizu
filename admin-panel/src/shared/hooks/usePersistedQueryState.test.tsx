import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import { MemoryRouter, useSearchParams } from 'react-router-dom';
import { usePersistedQueryState } from './usePersistedQueryState';

type Period = 'today' | 'week' | 'month';
const PERIODS: readonly Period[] = ['today', 'week', 'month'];
const asPeriod = (raw: unknown): Period | null =>
  PERIODS.includes(raw as Period) ? (raw as Period) : null;

const OPTIONS = {
  paramKey: 'period',
  storageKey: 'test:period',
  defaultValue: 'week' as Period,
  parse: asPeriod,
  serialize: (value: Period) => (value === 'week' ? null : value),
  validate: asPeriod,
};

function wrapper(entry: string) {
  return ({ children }: { children: ReactNode }) => (
    <MemoryRouter initialEntries={[entry]}>{children}</MemoryRouter>
  );
}

/** Renders the hook alongside the raw search params so the URL can be asserted. */
function renderPeriod(entry: string) {
  return renderHook(
    () => {
      const [value, setValue] = usePersistedQueryState(OPTIONS);
      const [params] = useSearchParams();
      return { value, setValue, params };
    },
    { wrapper: wrapper(entry) },
  );
}

describe('usePersistedQueryState', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it('prefers the URL param over the stored value', () => {
    localStorage.setItem('test:period', JSON.stringify('today'));
    const { result } = renderPeriod('/?period=month');
    expect(result.current.value).toBe('month');
  });

  it('falls back to the stored value when the URL is bare', () => {
    localStorage.setItem('test:period', JSON.stringify('today'));
    const { result } = renderPeriod('/');
    expect(result.current.value).toBe('today');
  });

  it('falls back to the default when neither URL nor storage has a value', () => {
    const { result } = renderPeriod('/');
    expect(result.current.value).toBe('week');
  });

  it('writes both the URL and localStorage on change', () => {
    const { result } = renderPeriod('/');
    act(() => { result.current.setValue('today'); });
    expect(result.current.value).toBe('today');
    expect(result.current.params.get('period')).toBe('today');
    expect(localStorage.getItem('test:period')).toBe(JSON.stringify('today'));
  });

  it('drops the param when the value returns to its default', () => {
    const { result } = renderPeriod('/?period=month');
    act(() => { result.current.setValue('week'); });
    expect(result.current.params.get('period')).toBeNull();
  });

  it('ignores an invalid URL param and falls back', () => {
    localStorage.setItem('test:period', JSON.stringify('today'));
    const { result } = renderPeriod('/?period=zzz');
    expect(result.current.value).toBe('today');
  });

  it('ignores a garbage stored value without throwing', () => {
    localStorage.setItem('test:period', '{broken');
    const { result } = renderPeriod('/');
    expect(result.current.value).toBe('week');
  });
});
