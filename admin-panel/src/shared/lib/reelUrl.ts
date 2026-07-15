/**
 * Build the public URL for a reel/short on its source platform.
 *
 * Returns `null` when the platform has no per-reel URL we can derive from the
 * id alone (e.g. Telegram needs a channel handle the engine doesn't store), so
 * callers can simply hide the link rather than render a broken one.
 */
export function reelUrl(platform: string, reelId: string): string | null {
  if (!reelId.trim()) return null;
  const id = encodeURIComponent(reelId);
  switch (platform.trim().toLowerCase()) {
    case 'instagram':
      return `https://www.instagram.com/reel/${id}/`;
    case 'youtube':
      return `https://www.youtube.com/shorts/${id}`;
    default:
      return null;
  }
}
