import { Card } from '@/shared/ui/Card';

interface LeadStatTileProps {
  readonly label: string;
  readonly value: string;
  readonly foot?: string;
}

/** One KPI tile in the Leads stat row (label on top, big number, footnote). */
export function LeadStatTile({ label, value, foot }: LeadStatTileProps) {
  return (
    <Card className="flex min-h-32 flex-col px-5 py-5">
      <div className="text-[11.5px] font-semibold text-text-faint">{label}</div>
      <div className="mt-auto font-head text-4xl font-extrabold tabular-nums leading-none">
        {value}
      </div>
      {foot ? <p className="mt-1.5 text-[11.5px] font-medium text-text-faint">{foot}</p> : null}
    </Card>
  );
}
