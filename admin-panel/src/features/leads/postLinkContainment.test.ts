import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative, sep } from 'node:path';
import { describe, expect, test } from 'vitest';
import { matchSchema, revealedLeadSchema } from '@/shared/schemas/panelState';
import { leadsToCsv } from '@/shared/selectors/leads';
import type { Match, RevealedLead } from '@/shared/types/domain';
import { buildMatch } from '@/test/fixtures';
import { leadsToExcelXml } from './exportLeads';

/**
 * THE INVARIANT: in the customer plane, a POST URL is not reachable AT ALL — not from a
 * lead row, not from the audited reveal, not from anywhere.
 *
 * This used to be the weaker claim "reachable only from a revealed lead". It was
 * tightened because the reveal itself was: an org now gets a lead's HANDLE and nothing
 * else, because the words the person wrote are superadmin-only. A post link would make
 * that promise hollow — the post is public and shows the comment in plain sight, so one
 * click reinstates by redirection exactly what dropping `text` closes. A pointer to the
 * comment is the comment.
 *
 * Why a structural test rather than a few per-component assertions: the absence of a
 * post pointer is a property of the WHOLE customer plane — every component, every
 * selector, every exporter — and a per-component test only ever proves it about the
 * components someone remembered to write one for. The next lead surface added would be
 * born unguarded.
 *
 * So this asserts it structurally: neither `Match` nor `RevealedLead` carries a post
 * pointer (type + wire), and no customer-plane file so much as names one.
 *
 * The superadmin plane (`features/admin/**`) is deliberately out of scope — seeing the
 * raw rows is its entire purpose.
 */

// From the project root, not `import.meta.url`: these specs run under jsdom, where
// `import.meta.url` is an http:// URL and `fileURLToPath` throws on it. Vitest always
// runs with the panel package root as cwd.
const SRC_ROOT = join(process.cwd(), 'src');

/** Directories excluded from the customer-plane scan, and why. */
const EXCLUDED_DIRS = [
  // The superadmin plane. IP-allowlisted, platform-admin only, and it is SUPPOSED to
  // show identity — that is the other half of the v27 decision.
  join('features', 'admin'),
  // Test doubles and fixtures. They model the wire, and a fixture naming a post is
  // how the "an older bridge still sends one" boundary cases above are written.
  'test',
];

/**
 * Files allowed to name a post pointer, with the reason each one is not a leak.
 * Anything else appearing here means a reel id has grown a home in the customer plane —
 * which is the failure this test exists to catch. Widen the product, not this list:
 * there is no longer any customer surface a post pointer legitimately belongs on.
 */
const ALLOWED = new Map<string, string>([
  // The superadmin plane's wire schemas. They live under `shared/` for import reasons
  // only; the payloads they describe (`/api/admin/*`) never reach a customer surface,
  // and keeping identity in them is the explicit other half of the v27 decision.
  [join('shared', 'schemas', 'admin.ts'), 'superadmin wire schemas'],
  // The builder itself. It has NO customer-plane importer any more (asserted below);
  // it survives as the one canonical place a post URL is ever constructed, so a future
  // superadmin surface that needs one does not hand-roll a second.
  [join('shared', 'lib', 'reelUrl.ts'), 'the URL builder itself — unimported by this plane'],
]);

/** Every non-test TS/TSX file in the customer plane, as a src-relative path. */
function customerPlaneFiles(dir: string = SRC_ROOT): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    const rel = relative(SRC_ROOT, full);
    if (EXCLUDED_DIRS.some((d) => rel === d || rel.startsWith(d + sep))) continue;
    if (statSync(full).isDirectory()) {
      out.push(...customerPlaneFiles(full));
      continue;
    }
    if (!/\.tsx?$/.test(entry) || /\.test\.tsx?$/.test(entry)) continue;
    out.push(rel);
  }
  return out;
}

/**
 * Source with comments removed, so the prose explaining this invariant (which naturally
 * names `reelId`) cannot itself trip the scan. The `//` rule skips `://` so a URL inside
 * a string literal is not mistaken for a line comment and truncated.
 */
function stripComments(source: string): string {
  return source
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/(^|[^:])\/\/[^\n]*/g, '$1');
}

describe('post-link containment — the customer plane has no route to a post URL', () => {
  test('neither the Match type nor the reveal answer carries a post pointer', () => {
    // Compile-time half: these lines stop type-checking the day `reelId` returns to
    // either type, which is the moment every runtime guard below becomes bypassable.
    type HasReelId<T> = 'reelId' extends keyof T ? true : false;
    const matchHasPostPointer: HasReelId<Match> = false;
    const revealHasPostPointer: HasReelId<RevealedLead> = false;
    expect([matchHasPostPointer, revealHasPostPointer]).toEqual([false, false]);
  });

  test('the reveal answer carries no comment body either', () => {
    // The handle is the WHOLE answer. `text` on this type would be the change undone.
    type HasText<T> = 'text' extends keyof T ? true : false;
    const revealHasComment: HasText<RevealedLead> = false;
    expect(revealHasComment).toBe(false);
  });

  test('the wire boundary strips a post pointer a rogue payload still sends', () => {
    const wire = {
      ...buildMatch({ commentId: 'c1' }),
      // What a bridge that predates the drop — or an attacker replaying an old response
      // shape — would send. `z.object` drops it before any component sees the lead.
      reelId: 'DXOML7vjQhn',
    };
    expect(matchSchema.parse(wire)).not.toHaveProperty('reelId');
  });

  test('the reveal boundary strips a comment body AND a post pointer', () => {
    // The same second line, on the other payload that used to carry both. A bridge
    // older than this change still answers with `text`/`reelId`; the schema is what
    // guarantees a stale deployment is not a way around the policy.
    const parsed = revealedLeadSchema.parse({
      id: 'cmp-001:instagram:c1', commentId: 'c1', platform: 'instagram',
      username: 'dana_t', text: 'how much?', reelId: 'DXOML7vjQhn',
    });
    expect(parsed).not.toHaveProperty('text');
    expect(parsed).not.toHaveProperty('reelId');
    expect(JSON.stringify(parsed)).not.toContain('how much?');
  });

  test('NOTHING in the customer plane names a post pointer', () => {
    const offenders = customerPlaneFiles()
      .filter((rel) => !ALLOWED.has(rel))
      .filter((rel) => /\breelUrl\b|\breelId\b/.test(stripComments(readFileSync(join(SRC_ROOT, rel), 'utf8'))));
    expect(offenders).toEqual([]);
  });

  test('the post-URL builder has no customer-plane importer at all', () => {
    // The strong form of the invariant, and the reason the drawer-specific call-site
    // assertion this replaced is gone: there is no call site to constrain. `reelUrl`
    // is unreachable from the customer plane, so no future edit can re-point one at a
    // lead row — it would have to add the import back, and this fails on that.
    const importers = customerPlaneFiles()
      .filter((rel) => rel !== join('shared', 'lib', 'reelUrl.ts'))
      .filter((rel) => /from\s+['"][^'"]*reelUrl['"]/.test(readFileSync(join(SRC_ROOT, rel), 'utf8')));
    expect(importers).toEqual([]);
  });

  test('the lead drawer renders no outbound platform link', () => {
    // Belt to the braces above: even a hand-built URL string — no `reelUrl`, no
    // `reelId` identifier, so invisible to the scans above — is caught here.
    const drawer = stripComments(readFileSync(join(SRC_ROOT, 'features', 'leads', 'LeadDrawer.tsx'), 'utf8'));
    expect(drawer).not.toMatch(/instagram\.com|youtube\.com|reddit\.com|t\.me|x\.com|linkedin\.com/);
    expect(drawer).not.toMatch(/target=["']_blank["']/);
  });

  test('no exporter can emit a post URL, revealed or not', () => {
    // Exports are the escape hatch that outlives the session, so they get their own
    // assertion rather than leaning on the type: a file on disk cannot be un-leaked.
    const leads = [
      buildMatch({ commentId: 'c1', platform: 'instagram', intent: 'Wants pricing for the Pro plan' }),
      buildMatch({ commentId: 'c2', platform: 'youtube', intent: 'Asking about delivery to Tashkent' }),
    ];
    for (const artifact of [leadsToCsv(leads), leadsToExcelXml(leads)]) {
      expect(artifact).toContain('Wants pricing for the Pro plan');
      expect(artifact).not.toMatch(/instagram\.com|youtube\.com|\/reel\/|\/shorts\//);
    }
  });
});
