/** Human display label for a platform key.
 *
 * Plain capitalization is wrong for a couple of platforms — "x" → "X" (not
 * lowercase) and "linkedin" → "LinkedIn" (camel-cased brand), "youtube" →
 * "YouTube". Everything else title-cases cleanly. Centralized here so chips,
 * selects, and tiles all render the same casing. */
const PLATFORM_LABELS: Readonly<Record<string, string>> = {
  instagram: 'Instagram',
  youtube: 'YouTube',
  telegram: 'Telegram',
  reddit: 'Reddit',
  x: 'X',
  linkedin: 'LinkedIn',
};

/** Display label for a platform key (never throws on an unknown platform). */
export function platformLabel(platform: string): string {
  const key = platform.toLowerCase();
  return PLATFORM_LABELS[key] ?? (platform.charAt(0).toUpperCase() + platform.slice(1));
}
