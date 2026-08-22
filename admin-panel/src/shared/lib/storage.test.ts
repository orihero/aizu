import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { readStored, writeStored } from './storage';

const KEY = 'test:key';
const acceptString = (raw: unknown): string | null => (typeof raw === 'string' ? raw : null);

describe('storage helpers', () => {
  beforeEach(() => {
    localStorage.clear();
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('returns null when the key is missing', () => {
    expect(readStored(KEY, acceptString)).toBeNull();
  });

  it('round-trips a written value through validation', () => {
    writeStored(KEY, 'hello');
    expect(readStored(KEY, acceptString)).toBe('hello');
  });

  it('returns null for malformed JSON without throwing', () => {
    localStorage.setItem(KEY, '{not valid json');
    expect(readStored(KEY, acceptString)).toBeNull();
  });

  it('returns null when the validator rejects the parsed shape', () => {
    writeStored(KEY, 42); // a number — rejected by acceptString
    expect(readStored(KEY, acceptString)).toBeNull();
  });

  it('swallows write errors (e.g. private mode / quota)', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('QuotaExceeded');
    });
    expect(() => { writeStored(KEY, 'x'); }).not.toThrow();
  });

  it('returns null when reading throws', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked');
    });
    expect(readStored(KEY, acceptString)).toBeNull();
  });
});

// An early, NAMED signal for the setup.ts storage shim (see src/test/setup.ts). If the shim
// ever silently no-ops, the suite still goes fully red via the global afterEach — this block
// does not replace that wipeout, it just says *why* in one readable failure. `toBeInstanceOf`
// is the load-bearing assertion: Node's own webstorage object is also constructor-named
// `Storage` but is not an instance of jsdom's, so this catches the silent-substitution
// variant (a working-but-wrong Storage) as well as the plain `undefined` one.
describe('test environment', () => {
  it('exposes the jsdom window Storage, not Node webstorage', () => {
    expect(localStorage).toBeInstanceOf(Storage);
    expect(sessionStorage).toBeInstanceOf(Storage);
  });
  it('round-trips and clears', () => {
    localStorage.setItem('k', 'v');
    expect(localStorage.getItem('k')).toBe('v');
    localStorage.clear();
    expect(localStorage.length).toBe(0);
  });
});
