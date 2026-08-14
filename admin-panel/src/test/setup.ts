import '@testing-library/jest-dom/vitest';

// Node >=25 puts its own `localStorage`/`sessionStorage` on globalThis, and vitest's jsdom
// environment refuses to copy any window key that already exists on the global
// (getWindowKeys: `if (k in global) return keysArray.includes(k)`, and neither storage name
// is in its KEYS allowlist), so jsdom's Storage never lands — `localStorage` stays Node's,
// which is `undefined` without --localstorage-file. Re-point both at the real jsdom window.
// vitest shadows `document.defaultView` with an own property pointing at the global, so read
// the untouched WebIDL accessor off Document.prototype. Do not name the getter in a variable:
// that trips @typescript-eslint/unbound-method under strictTypeChecked. On Node <=24 these are
// already the same objects, so this is a no-op. Remove once vitest ships vitest-dev/vitest#8757.
const defaultViewDescriptor = Object.getOwnPropertyDescriptor(Document.prototype, 'defaultView');
const jsdomWindow = defaultViewDescriptor?.get?.call(document) as Window | null | undefined;
if (jsdomWindow) {
  for (const key of ['localStorage', 'sessionStorage'] as const) {
    Object.defineProperty(globalThis, key, {
      value: jsdomWindow[key],
      writable: true,
      configurable: true,
    });
  }
}

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
