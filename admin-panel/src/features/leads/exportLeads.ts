import {
  LEAD_EXPORT_COLUMNS,
  LEAD_EXPORT_HEADERS,
  leadsToCsv,
  leadToExportRow,
} from '@/shared/selectors/leads';
import type { Match } from '@/shared/types/domain';

const REVOKE_DELAY_MS = 500;

/** The export formats offered on the Leads page. */
export type LeadExportFormat = 'csv' | 'excel' | 'pdf';

export const LEAD_EXPORT_FILENAME: Readonly<Record<LeadExportFormat, string>> = {
  csv: 'leads.csv',
  excel: 'leads.xls',
  pdf: 'leads.pdf',
};

/**
 * Trigger a client-side download of `blob` as `filename`. Shared by every
 * exporter so the anchor/object-URL lifecycle lives in one place.
 */
function triggerDownload(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => { URL.revokeObjectURL(url); }, REVOKE_DELAY_MS);
}

/** Download the given leads as a CSV file. No-op for an empty list. */
export function downloadLeadsCsv(leads: readonly Match[], filename = LEAD_EXPORT_FILENAME.csv): void {
  if (leads.length === 0) return;
  const blob = new Blob([leadsToCsv(leads)], { type: 'text/csv;charset=utf-8' });
  triggerDownload(blob, filename);
}

const XML_ESCAPE: Readonly<Record<string, string>> = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&apos;',
};

function escapeXml(value: string): string {
  return value.replace(/[&<>"']/g, (ch) => XML_ESCAPE[ch] ?? ch);
}

/** A SpreadsheetML cell — typed as a number only when the column is numeric and parses cleanly. */
function excelCell(value: string, numeric: boolean): string {
  if (numeric && value !== '' && !Number.isNaN(Number(value))) {
    return `<Cell><Data ss:Type="Number">${value}</Data></Cell>`;
  }
  return `<Cell><Data ss:Type="String">${escapeXml(value)}</Data></Cell>`;
}

/** The leads as a SpreadsheetML (Excel 2003 XML) workbook string. Exported for testing. */
export function leadsToExcelXml(leads: readonly Match[]): string {
  const toRow = (cells: string) => `<Row>${cells}</Row>`;
  const headerRow = toRow(LEAD_EXPORT_COLUMNS.map((c) => excelCell(c.header, false)).join(''));
  const bodyRows = leads
    .map((lead) => toRow(LEAD_EXPORT_COLUMNS.map((c) => excelCell(c.value(lead), c.numeric ?? false)).join('')))
    .join('');
  return `<?xml version="1.0"?>
<?mso-application progid="Excel.Sheet"?>
<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet" xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">
 <Worksheet ss:Name="Leads">
  <Table>${headerRow}${bodyRows}</Table>
 </Worksheet>
</Workbook>`;
}

/**
 * Download the given leads as an Excel file. Uses the SpreadsheetML 2003 XML
 * format — a real, typed Excel format that opens in Excel, Numbers, Google
 * Sheets, and LibreOffice with zero added dependencies. No-op for an empty list.
 */
export function downloadLeadsExcel(leads: readonly Match[], filename = LEAD_EXPORT_FILENAME.excel): void {
  if (leads.length === 0) return;
  const blob = new Blob([leadsToExcelXml(leads)], { type: 'application/vnd.ms-excel' });
  triggerDownload(blob, filename);
}

// Brand lime (#bef264-ish) header on the PDF table, dark ink text. RGB tuples
// because jspdf-autotable takes raw colour arrays, not CSS tokens.
const PDF_HEAD_FILL: [number, number, number] = [190, 242, 100];
const PDF_HEAD_TEXT: [number, number, number] = [26, 32, 24];

/**
 * Download the given leads as a PDF table. jsPDF + autotable are loaded lazily
 * (dynamic import) so they stay out of the main bundle until an export is run.
 * Rejects on failure so the caller can surface a message. No-op for an empty list.
 */
export async function downloadLeadsPdf(
  leads: readonly Match[],
  filename = LEAD_EXPORT_FILENAME.pdf,
): Promise<void> {
  if (leads.length === 0) return;
  const { jsPDF } = await import('jspdf');
  const { default: autoTable } = await import('jspdf-autotable');

  const doc = new jsPDF({ orientation: 'landscape' });
  doc.setFontSize(14);
  doc.text(`Leads export — ${leads.length} ${leads.length === 1 ? 'lead' : 'leads'}`, 14, 15);

  autoTable(doc, {
    head: [[...LEAD_EXPORT_HEADERS]],
    body: leads.map((lead) => [...leadToExportRow(lead)]),
    startY: 20,
    styles: { fontSize: 8, cellPadding: 2, overflow: 'linebreak' },
    headStyles: { fillColor: PDF_HEAD_FILL, textColor: PDF_HEAD_TEXT, fontStyle: 'bold' },
    columnStyles: { 6: { cellWidth: 90 } },
  });

  doc.save(filename);
}

/** Run the exporter for `format` against `leads`. Single entry point for the UI. */
export async function exportLeads(format: LeadExportFormat, leads: readonly Match[]): Promise<void> {
  switch (format) {
    case 'csv':
      downloadLeadsCsv(leads);
      return;
    case 'excel':
      downloadLeadsExcel(leads);
      return;
    case 'pdf':
      await downloadLeadsPdf(leads);
      return;
  }
}
