import { useState } from 'react';
import { Button } from '@/shared/ui/Button';
import { Modal } from '@/shared/ui/Modal';
import { platformLabel } from '@/shared/lib/platformLabel';
import type { EnqueueJobInput } from '@/shared/schemas/admin';
import { useEnqueueJob } from './adminHooks';

/** Platforms an operator can target — mirrors the engine's SUPPORTED_PLATFORMS. */
const PLATFORMS = ['instagram', 'youtube', 'telegram', 'reddit', 'x', 'linkedin'] as const;

/** Parse an optional positive integer field; empty → undefined, invalid → undefined. */
function optionalPositiveInt(raw: string): number | undefined {
  const trimmed = raw.trim();
  if (trimmed === '') return undefined;
  const value = Number(trimmed);
  return Number.isInteger(value) && value >= 1 ? value : undefined;
}

export function EnqueueJobModal({
  isOpen,
  onClose,
}: {
  readonly isOpen: boolean;
  readonly onClose: () => void;
}) {
  const enqueue = useEnqueueJob();
  const [campaignId, setCampaignId] = useState('');
  const [platform, setPlatform] = useState<string>(PLATFORMS[0]);
  const [orgId, setOrgId] = useState('');
  const [accountHandle, setAccountHandle] = useState('');
  const [engineMode, setEngineMode] = useState<'harvest' | 'warming'>('harvest');
  const [targetLeads, setTargetLeads] = useState('');
  const [error, setError] = useState<string | null>(null);

  const canSubmit = campaignId.trim() !== '';

  function reset() {
    setCampaignId('');
    setPlatform(PLATFORMS[0]);
    setOrgId('');
    setAccountHandle('');
    setEngineMode('harvest');
    setTargetLeads('');
    setError(null);
  }

  function submit() {
    setError(null);
    const orgValue = optionalPositiveInt(orgId);
    const leadsValue = optionalPositiveInt(targetLeads);
    const input: EnqueueJobInput = {
      campaignId: campaignId.trim(),
      platform,
      engineMode,
      ...(orgValue !== undefined ? { orgId: orgValue } : {}),
      ...(accountHandle.trim() !== '' ? { requiredAccountHandle: accountHandle.trim() } : {}),
      ...(leadsValue !== undefined ? { targetLeads: leadsValue } : {}),
    };
    enqueue.mutate(input, {
      onSuccess: () => {
        reset();
        onClose();
      },
      onError: (e: unknown) => {
        setError(e instanceof Error ? e.message : 'Failed to enqueue job');
      },
    });
  }

  const footer = (
    <>
      <Button variant="ghost" onClick={onClose} disabled={enqueue.isPending}>
        Cancel
      </Button>
      <Button onClick={submit} disabled={!canSubmit || enqueue.isPending}>
        Enqueue
      </Button>
    </>
  );

  return (
    <Modal isOpen={isOpen} onClose={onClose} title="Enqueue a job" footer={footer}>
      {error ? (
        <div role="alert" className="mb-3 rounded-tile bg-danger-soft px-3 py-2 text-[13px] text-danger">
          {error}
        </div>
      ) : null}
      <label className="block text-xs font-semibold text-text-muted" htmlFor="enq-campaign">
        Campaign id
      </label>
      <input
        id="enq-campaign"
        value={campaignId}
        onChange={(e) => { setCampaignId(e.target.value); }}
        className="mt-1 w-full rounded-tile border border-border bg-bg px-3 py-2 text-sm text-text outline-none focus:border-brand"
      />

      <div className="mt-3 grid grid-cols-2 gap-3">
        <div>
          <label className="block text-xs font-semibold text-text-muted" htmlFor="enq-platform">
            Platform
          </label>
          <select
            id="enq-platform"
            value={platform}
            onChange={(e) => { setPlatform(e.target.value); }}
            className="mt-1 w-full rounded-tile border border-border bg-bg px-3 py-2 text-sm text-text outline-none focus:border-brand"
          >
            {PLATFORMS.map((p) => (
              <option key={p} value={p}>
                {platformLabel(p)}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label className="block text-xs font-semibold text-text-muted" htmlFor="enq-mode">
            Mode
          </label>
          <select
            id="enq-mode"
            value={engineMode}
            onChange={(e) => { setEngineMode(e.target.value as 'harvest' | 'warming'); }}
            className="mt-1 w-full rounded-tile border border-border bg-bg px-3 py-2 text-sm text-text outline-none focus:border-brand"
          >
            <option value="harvest">harvest</option>
            <option value="warming">warming</option>
          </select>
        </div>
      </div>

      <div className="mt-3 grid grid-cols-2 gap-3">
        <div>
          <label className="block text-xs font-semibold text-text-muted" htmlFor="enq-org">
            Org id (optional)
          </label>
          <input
            id="enq-org"
            inputMode="numeric"
            value={orgId}
            onChange={(e) => { setOrgId(e.target.value); }}
            className="mt-1 w-full rounded-tile border border-border bg-bg px-3 py-2 text-sm text-text outline-none focus:border-brand"
          />
        </div>
        <div>
          <label className="block text-xs font-semibold text-text-muted" htmlFor="enq-leads">
            Target leads (optional)
          </label>
          <input
            id="enq-leads"
            inputMode="numeric"
            value={targetLeads}
            onChange={(e) => { setTargetLeads(e.target.value); }}
            className="mt-1 w-full rounded-tile border border-border bg-bg px-3 py-2 text-sm text-text outline-none focus:border-brand"
          />
        </div>
      </div>

      <label className="mt-3 block text-xs font-semibold text-text-muted" htmlFor="enq-account">
        Required account handle (optional)
      </label>
      <input
        id="enq-account"
        value={accountHandle}
        onChange={(e) => { setAccountHandle(e.target.value); }}
        className="mt-1 w-full rounded-tile border border-border bg-bg px-3 py-2 text-sm text-text outline-none focus:border-brand"
      />
    </Modal>
  );
}
