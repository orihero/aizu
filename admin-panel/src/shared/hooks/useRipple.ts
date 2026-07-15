import { useCallback, type PointerEvent } from 'react';
import { usePrefersReducedMotion } from './usePrefersReducedMotion';

/**
 * Returns a pointer-down handler that paints a Material-style ripple from the
 * click point. The host element must be `position: relative` and clip overflow.
 * No-op when the user prefers reduced motion.
 */
export function useRipple() {
  const reduced = usePrefersReducedMotion();

  return useCallback(
    (event: PointerEvent<HTMLElement>) => {
      if (reduced) return;
      const host = event.currentTarget;
      const rect = host.getBoundingClientRect();
      const size = Math.max(rect.width, rect.height);
      const ink = document.createElement('span');
      ink.className = 'ripple-ink';
      ink.style.width = `${size}px`;
      ink.style.height = `${size}px`;
      ink.style.left = `${event.clientX - rect.left - size / 2}px`;
      ink.style.top = `${event.clientY - rect.top - size / 2}px`;
      ink.addEventListener('animationend', () => {
        ink.remove();
      });
      host.appendChild(ink);
    },
    [reduced],
  );
}
