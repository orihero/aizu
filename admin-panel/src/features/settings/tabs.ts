import type { LucideIcon } from 'lucide-react';
import { Building2, CreditCard, Plug, Users } from 'lucide-react';

export type SettingsTab = 'workspace' | 'team' | 'integrations' | 'billing';

export interface TabDef {
  readonly key: SettingsTab;
  readonly label: string;
  readonly description: string;
  readonly icon: LucideIcon;
}

export const TABS: readonly TabDef[] = [
  {
    key: 'workspace',
    label: 'Workspace',
    description: 'Product name, timezone and engine thresholds.',
    icon: Building2,
  },
  { key: 'team', label: 'Team', description: 'Members, roles and invites.', icon: Users },
  {
    key: 'integrations',
    label: 'Integrations',
    description: 'Connected platforms and sources.',
    icon: Plug,
  },
  {
    key: 'billing',
    label: 'Billing',
    description: 'Plan, usage and invoices.',
    icon: CreditCard,
  },
];

const VALID = new Set<string>(TABS.map((t) => t.key));

/** Coerce a raw route param to a known tab, defaulting to 'workspace'. */
export function resolveTab(raw: string | undefined): SettingsTab {
  return raw && VALID.has(raw) ? (raw as SettingsTab) : 'workspace';
}
