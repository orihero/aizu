import { describe, expect, test } from 'vitest';
import { buildMatch } from '@/test/fixtures';
import { leadsToCsv } from '@/shared/selectors/leads';
import { leadsToExcelXml } from './exportLeads';

describe('leadsToExcelXml', () => {
  test('emits a SpreadsheetML workbook with a header row plus one row per lead', () => {
    const xml = leadsToExcelXml([
      buildMatch({ commentId: 'a', username: 'aziz' }),
      buildMatch({ commentId: 'b', username: 'bek' }),
    ]);

    expect(xml).toContain('<?mso-application progid="Excel.Sheet"?>');
    expect(xml).toContain('ss:Name="Leads"');
    // 1 header row + 2 body rows.
    expect(xml.match(/<Row>/g)).toHaveLength(3);
    expect(xml).toContain('aziz');
    expect(xml).toContain('bek');
  });

  test('writes the score column as a typed number, not a string', () => {
    const xml = leadsToExcelXml([buildMatch({ score: 0.91 })]);
    expect(xml).toContain('<Data ss:Type="Number">0.91</Data>');
  });

  test('xml-escapes special characters in text cells', () => {
    const xml = leadsToExcelXml([buildMatch({ text: 'a & b <c> "d"' })]);
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
