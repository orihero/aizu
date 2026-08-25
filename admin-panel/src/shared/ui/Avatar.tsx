import { cn } from '@/shared/lib/cn';

/**
 * v27: the prop is `name`, not `username`. This component was born on the leads table,
 * keyed off a commenter's handle — the surface the redaction removed it from. Its only
 * caller now is the team panel, where the string is a WORKSPACE MEMBER's own name.
 * The old prop name read as an invitation to hand it a lead's handle again; there is no
 * longer such a thing on an org-facing lead, and the drawer's revealed identity is
 * deliberately typographic, not an avatar.
 */
interface AvatarProps {
  /** A workspace person's display name or initials. Never a lead's identity. */
  readonly name: string;
  readonly size?: 'sm' | 'lg';
}

// Pulse-toned gradients (indigo / sky / teal / amber / pink / lime-leaning).
// Each pairs a deep base with a lighter accent so white initials stay legible.
const PALETTE = [
  'linear-gradient(135deg, #4f46e5 10%, #a78bfa 110%)',
  'linear-gradient(135deg, #0284c7 10%, #38bdf8 110%)',
  'linear-gradient(135deg, #0d9488 10%, #2dd4bf 110%)',
  'linear-gradient(135deg, #d97706 10%, #fbbf24 110%)',
  'linear-gradient(135deg, #db2777 10%, #f472b6 110%)',
  'linear-gradient(135deg, #4338ca 10%, #818cf8 110%)',
] as const;

function hashCode(value: string): number {
  let hash = 0;
  for (let i = 0; i < value.length; i += 1) {
    hash = (hash * 31 + value.charCodeAt(i)) | 0;
  }
  return Math.abs(hash);
}

function initials(name: string): string {
  const parts = name.split(/[._-]/).filter(Boolean);
  const first = parts[0]?.charAt(0) ?? '?';
  const second = parts[1]?.charAt(0) ?? '';
  return (first + second).toUpperCase();
}

export function Avatar({ name, size = 'sm' }: AvatarProps) {
  const background = PALETTE[hashCode(name) % PALETTE.length] ?? PALETTE[0];
  return (
    <span
      aria-hidden
      className={cn(
        'inline-flex shrink-0 items-center justify-center rounded-full font-head font-bold text-white ring-2 ring-white/25',
        size === 'lg' ? 'size-10 text-sm' : 'size-7 text-[10px]',
      )}
      style={{ background }}
    >
      {initials(name)}
    </span>
  );
}
