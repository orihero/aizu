import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative, sep } from 'node:path';
import { describe, expect, test } from 'vitest';
import { matchSchema } from '@/shared/schemas/panelState';
import { leadsToCsv } from '@/shared/selectors/leads';
import type { Match } from '@/shared/types/domain';
import { buildMatch } from '@/test/fixtures';
import { leadsToExcelXml } from './exportLeads';

/**
 * THE INVARIANT: in the customer plane, a POST URL is reachable only from a lead that
 * has been revealed through the audited `POST /api/lead/reveal`.
 *
 * Why this test exists rather than a few per-component assertions: the redaction hides a
 * handle and a comment, but the post those live on is PUBLIC. One reel id plus
 * `reelUrl()` reproduces both, so the redaction is only as strong as the absence of a
 * post pointer on the anonymized lead. That absence is a property of the whole customer
 * plane — every component, every selector, every exporter — and a per-component test
 * only ever proves it about the components someone remembered to write one for. The
 * next lead surface added would be born unguarded.
 *
 * So this asserts it structurally: `Match` has no post pointer at all (type + wire), and
 * the one module that can turn one into a URL is imported by exactly one file, whose
 * only call site is fed by a `RevealedLead`.
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
  // Test doubles and fixtures. The fake repository has to model the reveal ANSWER,
  // which legitimately carries a reel id.
  'test',
];

/**
 * Files allowed to name a post pointer, with the reason each one is not a leak.
 * Anything else appearing here means a reel id has grown a second home in the customer
 * plane — which is the failure this test exists to catch, so widen the product, not
 * this list, unless the new site is genuinely reveal-fed.
 */
const ALLOWED = new Map<string, string>([
  // The superadmin plane's wire schemas. They live under `shared/` for import reasons
  // only; the payloads they describe (`/api/admin/*`) never reach a customer surface,
  // and keeping identity in them is the explicit other half of the v27 decision.
  [join('shared', 'schemas', 'admin.ts'), 'superadmin wire schemas'],
  [join('shared', 'lib', 'reelUrl.ts'), 'the URL builder itself'],
  [join('shared', 'types', 'domain.ts'), 'the RevealedLead type'],
  [join('shared', 'schemas', 'panelState.ts'), 'revealedLeadSchema (the reveal answer)'],
  [join('features', 'leads', 'LeadDrawer.tsx'), 'the reveal UI — asserted below to be reveal-fed'],
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

describe('post-link containment — an unrevealed lead has no route to a post URL', () => {
  test('the Match type carries no post pointer', () => {
    // Compile-time half: this line stops type-checking the day `reelId` returns to
    // `Match`, which is the moment every runtime guard below becomes bypassable.
    type HasReelId<T> = 'reelId' extends keyof T ? true : false;
    const matchHasPostPointer: HasReelId<Match> = false;
    expect(matchHasPostPointer).toBe(false);
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

  test('only the reveal UI can reach the post-URL builder', () => {
    const offenders = customerPlaneFiles()
      .filter((rel) => !ALLOWED.has(rel))
      .filter((rel) => /\breelUrl\b|\breelId\b/.test(stripComments(readFileSync(join(SRC_ROOT, rel), 'utf8'))));
    expect(offenders).toEqual([]);
  });

  test("the drawer's post link is fed by the reveal answer, never by the lead row", () => {
    const drawer = readFileSync(join(SRC_ROOT, 'features', 'leads', 'LeadDrawer.tsx'), 'utf8');
    // `<ReelLink .../>` is the sole consumer of `reelUrl` in the customer app. Every one
    // of its call sites must read `state.source` — the RevealedLead — so a future edit
    // cannot quietly re-point it at the `lead` prop that is in scope right beside it.
    const callSites = [...drawer.matchAll(/<ReelLink\b[^>]*\/>/g)].map((m) => m[0]);
    expect(callSites).toHaveLength(1);
    for (const site of callSites) {
      expect(site).toContain('state.source.reelId');
      expect(site).not.toMatch(/\blead\./);
    }
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
