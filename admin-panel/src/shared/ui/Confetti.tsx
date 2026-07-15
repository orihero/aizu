import { useMemo } from 'react';

/**
 * Dependency-free celebratory confetti burst. Renders a fixed number of absolutely
 * positioned, CSS-animated pieces over its (relatively positioned) parent. Purely
 * decorative — aria-hidden — and it disables itself under prefers-reduced-motion via
 * the `motion-reduce:hidden` utility so it never animates for motion-sensitive users.
 */

const COLORS = ['#22c55e', '#84cc16', '#0ea5e9', '#f59e0b', '#ec4899', '#8b5cf6'];
const PIECE_COUNT = 42;

// A deterministic pseudo-random so a re-render doesn't reshuffle mid-animation.
function rand(seed: number): number {
  const x = Math.sin(seed * 99.13) * 43758.5453;
  return x - Math.floor(x);
}

export function Confetti({ pieceCount = PIECE_COUNT }: { readonly pieceCount?: number }) {
  const pieces = useMemo(
    () =>
      Array.from({ length: pieceCount }, (_, i) => {
        const left = rand(i + 1) * 100;
        const delay = rand(i + 2) * 0.5;
        const duration = 1.6 + rand(i + 3) * 1.4;
        const drift = (rand(i + 4) - 0.5) * 120;
        const color = COLORS[i % COLORS.length];
        const size = 6 + Math.floor(rand(i + 5) * 6);
        const round = rand(i + 6) > 0.5;
        return { i, left, delay, duration, drift, color, size, round };
      }),
    [pieceCount],
  );

  return (
    <div
      aria-hidden
      className="pointer-events-none absolute inset-0 overflow-hidden motion-reduce:hidden"
    >
      {pieces.map((p) => (
        <span
          key={p.i}
          className="absolute top-0 animate-[confetti-fall_var(--dur)_ease-in_var(--delay)_forwards]"
          style={
            {
              left: `${p.left}%`,
              width: `${p.size}px`,
              height: `${p.size}px`,
              backgroundColor: p.color,
              borderRadius: p.round ? '9999px' : '2px',
              '--dur': `${p.duration}s`,
              '--delay': `${p.delay}s`,
              '--drift': `${p.drift}px`,
            } as React.CSSProperties
          }
        />
      ))}
    </div>
  );
}
