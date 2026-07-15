import { useEffect } from 'react';

type HotkeyMap = Readonly<Record<string, () => void>>;

const EDITABLE_TAGS = new Set(['INPUT', 'TEXTAREA', 'SELECT']);

/** Binds single-key shortcuts, ignoring modifier combos and form fields. */
export function useHotkeys(hotkeys: HotkeyMap, enabled = true): void {
  useEffect(() => {
    if (!enabled) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      const target = event.target as HTMLElement | null;
      if (target && EDITABLE_TAGS.has(target.tagName)) return;
      const handler = hotkeys[event.key.toLowerCase()];
      if (!handler) return;
      event.preventDefault();
      handler();
    };
    document.addEventListener('keydown', onKeyDown);
    return () => { document.removeEventListener('keydown', onKeyDown); };
  }, [hotkeys, enabled]);
}
