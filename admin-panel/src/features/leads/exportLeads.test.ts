import { beforeEach, describe, expect, test, vi } from 'vitest';
import { buildMatch, buildPanelState } from '@/test/fixtures';
import { FakePanelRepository } from '@/test/fakePanelRepository';
import { LEAD_EXPORT_HEADERS, leadsToCsv } from '@/shared/selectors/leads';
import { downloadLeadsPdf, leadsToExcelXml } from './exportLeads';

/**
 * The PDF exporter goes through jsPDF + autotable, which cannot render in jsdom. Stub
 * both so the test can read the exact `head`/`body` matrix handed to the table — the
 * only place a PDF's cell contents exist before the binary is written.
 */
const pdfTables: { head: unknown[][]; body: unknown[][] }[] = [];
const pdfTitles: string[] = [];
vi.mock('jspdf', () => ({
  jsPDF: class {
    setFontSize() {}
    text(value: string) { pdfTitles.push(value); }
    save() {}
  },
}));
vi.mock('jspdf-autotable', () => ({
  default: (_doc: unknown, opts: { head: unknown[][]; body: unknown[][] }) => {
    pdfTables.push({ head: opts.head, body: opts.body });
  },
}));

describe('leadsToExcelXml', () => {
  test('emits a SpreadsheetML workbook with a header row plus one row per lead', () => {
    const xml = leadsToExcelXml([
      buildMatch({ commentId: 'a', intent: 'Wants a demo in Tashkent' }),
      buildMatch({ commentId: 'b', intent: 'Asking about bulk pricing' }),
    ]);

    expect(xml).toContain('<?mso-application progid="Excel.Sheet"?>');
    expect(xml).toContain('ss:Name="Leads"');
    // 1 header row + 2 body rows.
    expect(xml.match(/<Row>/g)).toHaveLength(3);
    expect(xml).toContain('Wants a demo in Tashkent');
    expect(xml).toContain('Asking about bulk pricing');
  });

  test('writes the score column as a typed number, not a string', () => {
    const xml = leadsToExcelXml([buildMatch({ score: 0.91 })]);
    expect(xml).toContain('<Data ss:Type="Number">0.91</Data>');
  });

  test('xml-escapes special characters in text cells', () => {
    const xml = leadsToExcelXml([buildMatch({ intent: 'a & b <c> "d"' })]);
    expect(xml).toContain('a &amp; b &lt;c&gt; &quot;d&quot;');
    expect(xml).not.toContain('<c>');
  });
});

describe('export column parity', () => {
  test('CSV and Excel expose the same column count for the same lead', () => {
    const lead = buildMatch();
    const csvRow = leadsToCsv([lead]).split('\n')[1] ?? '';
    const csvCells = csvRow.split('","').length;
    const excelCells = (leadsToExcelXml([lead]).match(/<Cell>/g) ?? []).length;
    // CSV has one row's cells; Excel counts header + body, so body == total / 2.
    expect(excelCells / 2).toBe(csvCells);
  });
});

/**
 * v27 + Section F. An export is a customer-facing artifact that leaves the app and
 * outlives the session, so it is the one surface where a leak is permanent and at
 * scale. These tests pin the two halves of that: the columns carry no identity, and a
 * reveal performed elsewhere in the session cannot bleed into the bytes an export
 * writes — `exportLeads` builds from the anonymized list and reads no reveal state.
 */
describe('exports never carry lead identity', () => {
  beforeEach(() => {
    pdfTables.length = 0;
    pdfTitles.length = 0;
  });

  test('the columns are intent-only — no username, no comment text', () => {
    expect(LEAD_EXPORT_HEADERS).toContain('intent');
    expect(LEAD_EXPORT_HEADERS).not.toContain('username');
    expect(LEAD_EXPORT_HEADERS).not.toContain('text');
  });

  test('an un-derived intent exports as an empty cell, never a stand-in identifier', () => {
    const lead = buildMatch({ commentId: 'c-empty', intent: '' });
    const row = leadsToCsv([lead]).split('\n')[1] ?? '';
    // The id column still carries the raw comment id; the intent cell beside it is
    // genuinely blank rather than a placeholder — or worse, a fallback to something
    // that identifies the person.
    expect(row).toContain('"c-empty"');
    expect(row).toContain('"c-empty","",');
    expect(row).not.toContain('Intent not captured');
  });

  test('a reveal earlier in the session does not reach the exported rows', async () => {
    const lead = buildMatch({ commentId: 'c1', intent: 'Wants pricing for the Pro plan' });
    const repository = new FakePanelRepository(buildPanelState({ MATCHES: [lead] }));
    repository.currentUser = {
      id: 1,
      email: 'owner@aizu.test',
      role: 'owner',
      orgId: 1,
      org: { id: 1, name: 'Test Co', logo: null, description: null },
    };
    // The handle is the WHOLE reveal answer now — there is no comment body to register,
    // because the bridge does not send one to a customer. So this test asserts what is
    // still assertable: the one identity field a reveal CAN put in this process must
    // not find its way from component state into a file on disk.
    repository.revealIdentities.set(lead.id, { username: 'dana_t' });

    const revealed = await repository.revealLead({
      campaignId: lead.campaignId,
      platform: lead.platform,
      commentId: lead.commentId,
    });
    expect(revealed.ok).toBe(true);

    // Same anonymized list the page holds — the only thing an exporter is ever given.
    const csv = leadsToCsv([lead]);
    const xml = leadsToExcelXml([lead]);
    await downloadLeadsPdf([lead]);
    const pdf = JSON.stringify(pdfTables.at(-1));
    for (const artifact of [csv, xml, pdf]) {
      expect(artifact).toContain('Wants pricing for the Pro plan');
      expect(artifact).not.toContain('dana_t');
    }
    // ...and the reveal answer itself has no comment body to leak in the first place.
    if (revealed.ok) {
      expect(revealed.value).not.toHaveProperty('text');
      expect(revealed.value).not.toHaveProperty('reelId');
    }
  });

  test('the PDF table carries the intent column and no identity column', async () => {
    // The PDF is the one export whose cells never exist as a string in this process,
    // so it is also the one that could silently keep an old column. Read the matrix
    // handed to autotable rather than trusting that it shares LEAD_EXPORT_COLUMNS.
    await downloadLeadsPdf([
      buildMatch({ commentId: 'p1', intent: 'Wants a demo in Tashkent' }),
      buildMatch({ commentId: 'p2', intent: 'Asking for team-plan pricing' }),
    ]);

    const table = pdfTables.at(-1);
    expect(table).toBeDefined();
    expect(table?.head[0]).toContain('intent');
    expect(table?.head[0]).not.toContain('username');
    expect(table?.head[0]).not.toContain('text');
    expect(table?.body).toHaveLength(2);
    expect(table?.body[0]).toContain('Wants a demo in Tashkent');
    // Every body row is exactly as wide as the header — no stray identity cell.
    for (const row of table?.body ?? []) {
      expect(row).toHaveLength(LEAD_EXPORT_HEADERS.length);
    }
  });

  test('an empty list writes no PDF at all (nothing to leak, nothing to open)', async () => {
    const before = pdfTables.length;
    await downloadLeadsPdf([]);
    expect(pdfTables).toHaveLength(before);
  });
});
