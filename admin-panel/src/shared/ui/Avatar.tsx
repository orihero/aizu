import { cn } from '@/shared/lib/cn';

interface AvatarProps {
  readonly username: string;
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

function initials(username: string): string {
  const parts = username.split(/[._-]/).filter(Boolean);
  const first = parts[0]?.charAt(0) ?? '?';
  const second = parts[1]?.charAt(0) ?? '';
  return (first + second).toUpperCase();
}

export function Avatar({ username, size = 'sm' }: AvatarProps) {
  const background = PALETTE[hashCode(username) % PALETTE.length] ?? PALETTE[0];
  return (
    <span
      aria-hidden
      className={cn(
        'inline-flex shrink-0 items-center justify-center rounded-full font-head font-bold text-white ring-2 ring-white/25',
        size === 'lg' ? 'size-10 text-sm' : 'size-7 text-[10px]',
      )}
      style={{ background }}
    >
      {initials(username)}
    </span>
  );
}
