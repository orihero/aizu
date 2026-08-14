/**
 * A lead's identity is the engine's `matches` composite primary key
 * `(campaign_id, platform, comment_id)` — NOT the bare `commentId`. A comment id is
 * only unique inside one platform's id namespace, and the same commenter can appear
 * under two campaigns, so keying the UI on `commentId` alone collapsed two real leads
 * into one row: clicking a lead in campaign A opened, and wrote status to, campaign B's
 * lead.
 *
 * The encoding is a `|`-joined triple with `%` and `|` escaped inside each part, which
 * makes it injective (distinct triples never collide) and safe in a URL path segment.
 * `engine/aizu/panel.py::lead_uid` implements the SAME encoding character-for-character
 * — keep the two in lockstep.
 *
 * Treat the value as OPAQUE: never parse it back apart. Every write resolves the
 * composite key from the record's own `campaignId` / `platform` / `commentId` fields,
 * which is what makes a status write land on the record the operator actually clicked.
 */

function escapePart(part: string): string {
  return part.replace(/%/g, '%25').replace(/\|/g, '%7C');
}

export function leadUid(campaignId: string, platform: string, commentId: string): string {
  return [campaignId, platform, commentId].map(escapePart).join('|');
}

/** Identity of anything lead-shaped (a Match, an admin lead row, a fixture seed). */
export function leadUidOf(lead: {
  readonly campaignId: string;
  readonly platform: string;
  readonly commentId: string;
}): string {
  return leadUid(lead.campaignId, lead.platform, lead.commentId);
}

/**
 * The `/leads/:leadId` path for a lead. Encoded because the id is opaque and may
 * contain any character a campaign or comment id can; react-router decodes the param
 * back to the exact id on the way in.
 */
export function leadRoute(leadId: string): string {
  return `/leads/${encodeURIComponent(leadId)}`;
}
