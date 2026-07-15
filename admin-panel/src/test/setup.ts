import '@testing-library/jest-dom/vitest';

// recharts measures its container via ResizeObserver — absent in jsdom.
class ResizeObserverStub {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

if (!('ResizeObserver' in globalThis)) {
  Object.defineProperty(globalThis, 'ResizeObserver', { value: ResizeObserverStub });
}

// Persisted UI state (e.g. the Leads page writes its filter slice + view to
// localStorage) must not leak between tests — a stored campaign/status filter
// would silently scope a later test's LeadsPage and hide its seeded rows.
afterEach(() => {
  localStorage.clear();
});
