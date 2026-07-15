interface BrandMarkProps {
  /**
   * Where the mark sits, which decides its colors:
   * - `rail`   — on the always-dark nav rail: lime dot + white arc.
   * - `canvas` — on the themed page canvas (auth shell): brand dot + lime
   *              arc via tokens, so it adapts to light/dark automatically.
   */
  readonly tone?: 'rail' | 'canvas';
  readonly className?: string;
}

/**
 * The AIZU "ping" — a lead (the dot) and a single cue arc breaking off it.
 * A signal to act, not a radar sweep. Decorative, so it's hidden from a11y.
 */
export function BrandMark({ tone = 'rail', className }: BrandMarkProps) {
  const dot = tone === 'rail' ? '#d9f24f' : 'var(--color-brand)';
  const arc = tone === 'rail' ? '#ffffff' : 'var(--color-accent)';
  return (
    <svg viewBox="0 0 100 100" className={className} aria-hidden focusable="false">
      <circle cx="40" cy="60" r="16" fill={dot} />
      <path
        d="M47 31 A31 31 0 0 1 71 72"
        fill="none"
        stroke={arc}
        strokeWidth="8.5"
        strokeLinecap="round"
      />
    </svg>
  );
}
