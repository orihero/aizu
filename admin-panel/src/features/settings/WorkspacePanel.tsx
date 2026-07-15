import { useState } from 'react';
import { Check } from 'lucide-react';
import { Card, CardBody, CardHeader } from '@/shared/ui/Card';
import { Button } from '@/shared/ui/Button';
import { useUpdateOrganization, useUpdateSettings } from '@/shared/hooks/useWriteMutations';
import type { OrganizationInput, PanelConfig } from '@/shared/types/domain';
import { Field, INPUT_CLASS } from './SettingsField';

interface WorkspacePanelProps {
  readonly config: PanelConfig;
}

/** The company identity (v7) — name (required), optional logo + description. */
function CompanyCard({ config }: WorkspacePanelProps) {
  const org = config.organization;
  const [name, setName] = useState(org?.name ?? '');
  const [logo, setLogo] = useState(org?.logo ?? '');
  const [description, setDescription] = useState(org?.description ?? '');
  const mutation = useUpdateOrganization();

  const trimmedName = name.trim();
  const dirty =
    trimmedName !== (org?.name ?? '') ||
    logo.trim() !== (org?.logo ?? '') ||
    description.trim() !== (org?.description ?? '');
  const canSave = trimmedName.length > 0 && dirty && !mutation.isPending;

  const onSave = () => {
    if (!canSave) return;
    const input: OrganizationInput = {
      name: trimmedName,
      ...(logo.trim() ? { logo: logo.trim() } : {}),
      ...(description.trim() ? { description: description.trim() } : {}),
    };
    mutation.mutate(input);
  };

  return (
    <Card>
      <CardHeader title="Company" subtitle="How your company appears across the workspace." />
      <CardBody className="flex flex-col gap-5">
        <div className="grid gap-5 sm:grid-cols-2">
          <Field label="Company name" htmlFor="org-name">
            <input
              id="org-name"
              type="text"
              className={INPUT_CLASS}
              value={name}
              onChange={(e) => { setName(e.target.value); }}
            />
          </Field>
          <Field label="Logo URL" htmlFor="org-logo" hint="Optional.">
            <input
              id="org-logo"
              type="url"
              className={INPUT_CLASS}
              value={logo}
              placeholder="https://…/logo.png"
              onChange={(e) => { setLogo(e.target.value); }}
            />
          </Field>
        </div>
        <Field label="Description" htmlFor="org-desc" hint="A short description of what your company does.">
          <textarea
            id="org-desc"
            rows={2}
            className={`${INPUT_CLASS} resize-none`}
            value={description}
            onChange={(e) => { setDescription(e.target.value); }}
          />
        </Field>
        <div className="flex items-center gap-3 border-t border-border pt-4">
          <Button onClick={onSave} disabled={!canSave}>
            {mutation.isPending ? 'Saving…' : 'Save company'}
          </Button>
          {mutation.isError ? (
            <span className="text-xs font-medium text-danger">
              {mutation.error instanceof Error ? mutation.error.message : 'Save failed.'}
            </span>
          ) : mutation.isSuccess && !dirty ? (
            <span className="inline-flex items-center gap-1 text-xs font-medium text-success">
              <Check className="size-3.5" aria-hidden />
              Saved
            </span>
          ) : null}
        </div>
      </CardBody>
    </Card>
  );
}

/** The editable subset of CONFIG, all keys on the server-side whitelist. */
interface FormState {
  readonly productName: string;
  readonly timezone: string;
  readonly matchThreshold: number;
  readonly skipRatioThreshold: number;
  readonly budgetCapUsd: number;
}

function seed(config: PanelConfig): FormState {
  return {
    productName: config.productName,
    timezone: config.timezone,
    matchThreshold: config.matchThreshold,
    skipRatioThreshold: config.skipRatioThreshold,
    budgetCapUsd: config.budgetCapUsd,
  };
}

/** Collect only fields that differ from the seed — unknown keys would fail server-side. */
function changedFields(form: FormState, base: FormState): Record<string, unknown> {
  const changed: Record<string, unknown> = {};
  (Object.keys(form) as (keyof FormState)[]).forEach((key) => {
    if (form[key] !== base[key]) changed[key] = form[key];
  });
  return changed;
}

export function WorkspacePanel({ config }: WorkspacePanelProps) {
  const base = seed(config);
  const [form, setForm] = useState<FormState>(base);
  const mutation = useUpdateSettings();

  const update = <K extends keyof FormState>(key: K, value: FormState[K]) =>
    { setForm((prev) => ({ ...prev, [key]: value })); };

  const dirty = changedFields(form, base);
  const hasChanges = Object.keys(dirty).length > 0;

  const onSave = () => {
    if (!hasChanges) return;
    mutation.mutate({ settings: dirty });
  };

  // Numeric inputs: keep state numeric, fall back to 0 on an empty/invalid entry.
  const onNumber = (key: 'matchThreshold' | 'skipRatioThreshold' | 'budgetCapUsd') =>
    (raw: string) => {
      const parsed = Number(raw);
      update(key, Number.isFinite(parsed) ? parsed : 0);
    };

  return (
    <div className="flex flex-col gap-5">
      <CompanyCard config={config} />
      <Card>
      <CardHeader
        title="Workspace"
        subtitle="Engine thresholds that gate matching and spend."
      />
      <CardBody className="flex flex-col gap-5">
        <div className="grid gap-5 sm:grid-cols-2">
          <Field label="Product name" htmlFor="ws-product">
            <input
              id="ws-product"
              type="text"
              className={INPUT_CLASS}
              value={form.productName}
              onChange={(e) => { update('productName', e.target.value); }}
            />
          </Field>
          <Field label="Timezone" htmlFor="ws-tz" hint="IANA name, e.g. America/New_York.">
            <input
              id="ws-tz"
              type="text"
              className={INPUT_CLASS}
              value={form.timezone}
              onChange={(e) => { update('timezone', e.target.value); }}
            />
          </Field>
          <Field
            label="Match threshold"
            htmlFor="ws-match"
            hint="Minimum score (0–1) for a comment to count as a lead."
          >
            <input
              id="ws-match"
              type="number"
              step="any"
              min={0}
              max={1}
              className={`${INPUT_CLASS} tabular`}
              value={form.matchThreshold}
              onChange={(e) => { onNumber('matchThreshold')(e.target.value); }}
            />
          </Field>
          <Field
            label="Skip-ratio threshold"
            htmlFor="ws-skip"
            hint="Feed skip ratio (0–1) that flags a session as off-target."
          >
            <input
              id="ws-skip"
              type="number"
              step="any"
              min={0}
              max={1}
              className={`${INPUT_CLASS} tabular`}
              value={form.skipRatioThreshold}
              onChange={(e) => { onNumber('skipRatioThreshold')(e.target.value); }}
            />
          </Field>
          <Field label="Budget cap (USD)" htmlFor="ws-budget" hint="Daily spend ceiling for the engine.">
            <input
              id="ws-budget"
              type="number"
              step="any"
              min={0}
              className={`${INPUT_CLASS} tabular`}
              value={form.budgetCapUsd}
              onChange={(e) => { onNumber('budgetCapUsd')(e.target.value); }}
            />
          </Field>
        </div>

        <div className="flex items-center gap-3 border-t border-border pt-4">
          <Button onClick={onSave} disabled={!hasChanges || mutation.isPending}>
            {mutation.isPending ? 'Saving…' : 'Save changes'}
          </Button>
          {mutation.isError ? (
            <span className="text-xs font-medium text-danger">
              {mutation.error instanceof Error ? mutation.error.message : 'Save failed.'}
            </span>
          ) : mutation.isSuccess && !hasChanges ? (
            <span className="inline-flex items-center gap-1 text-xs font-medium text-success">
              <Check className="size-3.5" aria-hidden />
              Saved
            </span>
          ) : hasChanges ? (
            <span className="text-xs text-text-faint">Unsaved changes</span>
          ) : null}
        </div>
      </CardBody>
      </Card>
    </div>
  );
}
