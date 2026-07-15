import { Badge } from '@/shared/ui/Badge';
import { LEAD_STATUS_LABEL, LEAD_STATUS_TONE } from '@/shared/selectors/leads';
import type { MatchStatus } from '@/shared/types/domain';

/**
 * Status pill for the v6 Kanban pipeline. Label + tone come from the shared maps
 * in selectors/leads.ts so the pill, board, and dashboard never drift apart.
 */
export function LeadStatusPill({ status }: { readonly status: MatchStatus }) {
  return <Badge tone={LEAD_STATUS_TONE[status]}>{LEAD_STATUS_LABEL[status]}</Badge>;
}
