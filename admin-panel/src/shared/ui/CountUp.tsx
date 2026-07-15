import { useEffect, useRef, useState } from 'react';
import { usePrefersReducedMotion } from '@/shared/hooks/usePrefersReducedMotion';

interface CountUpProps {
  readonly value: number;
  /** Formats the (possibly fractional) in-flight value for display. */
  readonly format: (n: number) => string;
  readonly durationMs?: number;
  readonly className?: string;
}

const easeOutCubic = (t: number): number => 1 - Math.pow(1 - t, 3);

/**
 * Animates a number from its previous value to the new one with an ease-out
 * ramp, re-running on change. Jumps straight to the value under reduced motion.
 */
export function CountUp({ value, format, durationMs = 1000, className }: CountUpProps) {
  const reduced = usePrefersReducedMotion();
  const [display, setDisplay] = useState(value);
  const fromRef = useRef(0);
  const rafRef = useRef<number | undefined>(undefined);

  useEffect(() => {
    if (reduced) {
      setDisplay(value);
      fromRef.current = value;
      return;
    }
    const from = fromRef.current;
    const start = performance.now();
    const tick = (now: number) => {
      const progress = Math.min(1, (now - start) / durationMs);
      setDisplay(from + (value - from) * easeOutCubic(progress));
      if (progress < 1) {
        rafRef.current = requestAnimationFrame(tick);
      } else {
        fromRef.current = value;
      }
    };
    rafRef.current = requestAnimationFrame(tick);
    return () => {
      if (rafRef.current !== undefined) cancelAnimationFrame(rafRef.current);
      fromRef.current = value;
    };
  }, [value, durationMs, reduced]);

  return <span className={className}>{format(display)}</span>;
}
