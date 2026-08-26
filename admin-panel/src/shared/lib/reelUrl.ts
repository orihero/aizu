/**
 * Build the public URL for a reel/short on its source platform.
 *
 * Returns `null` when the platform has no per-reel URL we can derive from the
 * id alone (e.g. Telegram needs a channel handle the engine doesn't store), so
 * callers can simply hide the link rather than render a broken one.
 *
 * IT HAS NO CALLERS, AND THAT IS THE POINT — `postLinkContainment.test.ts` asserts it.
 * No LEAD in the customer plane holds a reel id (not on a lead row, not on the audited
 * reveal's answer), because the post this builds a link to is public and prints the
 * lead's comment in plain sight — and the comment is superadmin-only. So nothing in the
 * customer plane may turn a LEAD into a post URL.
 *
 * That is narrower than "the customer plane has no post ids", which would be false:
 * `reelSchema.id`/`thumbSeed` carry the scanned watchlist's real post ids (the A7
 * contract — the posts the agent read ARE the product), and they are deliberately not
 * joined to any lead. The containment test guards the LEAD→post edge, which is the one
 * that would undo the redaction.
 *
 * It survives unimported rather than deleted so that a future SUPERADMIN surface — the
 * one plane that does receive `reelId` — has one canonical place a post URL is built,
 * instead of a hand-rolled template literal that nobody thought to contain. Importing it
 * from anywhere outside `features/admin/**` fails that test, which is the intended
 * outcome: needing this function in the customer plane means something upstream already
 * went wrong.
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
