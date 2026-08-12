import { useState } from 'react';
import { Check, Copy } from 'lucide-react';
import { Button } from '@/shared/ui/Button';
import { Modal } from '@/shared/ui/Modal';
import type { EnrolmentScopeKind, MintEnrolmentTokenResult } from '@/shared/schemas/admin';
import { useAdminOrgs, useMintEnrolmentToken } from './adminHooks';

const DEFAULT_TTL_HOURS = 168; // 7 days — matches the server's DEFAULT_ENROLMENT_TTL_HOURS.

/** Parse an optional positive integer field; empty → undefined, invalid → undefined
 * (the server rejects an out-of-range ttlHours rather than clamping it). */
function optionalPositiveInt(raw: string): number | undefined {
  const trimmed = raw.trim();
  if (trimmed === '') return undefined;
  const value = Number(trimmed);
  return Number.isInteger(value) && value >= 1 ? value : undefined;
}

/**
 * Mint a per-worker enrolment token. The plaintext is returned by the mutation
 * exactly once (never persisted, never refetchable) — once minted, this modal
 * swaps to a copy-once display and the underlying form is gone for good.
 */
export function MintEnrolmentTokenModal({
  isOpen,
  onClose,
}: {
  readonly isOpen: boolean;
  readonly onClose: () => void;
}) {
  const mint = useMintEnrolmentToken();
  const orgs = useAdminOrgs();
  const [scope, setScope] = useState<EnrolmentScopeKind>('org');
  const [orgId, setOrgId] = useState('');
  const [label, setLabel] = useState('');
  const [ttlHours, setTtlHours] = useState(String(DEFAULT_TTL_HOURS));
  const [error, setError] = useState<string | null>(null);
  const [minted, setMinted] = useState<MintEnrolmentTokenResult | null>(null);
  const [copied, setCopied] = useState(false);

  const canSubmit = scope === 'pool' || orgId.trim() !== '';

  function reset() {
    setScope('org');
    setOrgId('');
    setLabel('');
    setTtlHours(String(DEFAULT_TTL_HOURS));
    setError(null);
    setMinted(null);
    setCopied(false);
  }

  function handleClose() {
    reset();
    onClose();
  }

  function submit() {
    setError(null);
    const ttlValue = optionalPositiveInt(ttlHours);
    mint.mutate(
      {
        scope,
        ...(scope === 'org' ? { orgId: Number(orgId) } : {}),
        ...(label.trim() !== '' ? { label: label.trim() } : {}),
        ...(ttlValue !== undefined ? { ttlHours: ttlValue } : {}),
      },
      {
        onSuccess: (result) => {
          setMinted(result);
        },
        onError: (e: unknown) => {
          setError(e instanceof Error ? e.message : 'Failed to mint token');
        },
      },
    );
  }

  function onCopy() {
    if (!minted) return;
    void navigator.clipboard.writeText(minted.token).then(() => {
      setCopied(true);
    });
  }

  const footer = minted ? (
    <Button onClick={handleClose}>Done</Button>
  ) : (
    <>
      <Button variant="ghost" onClick={handleClose} disabled={mint.isPending}>
        Cancel
      </Button>
      <Button onClick={submit} disabled={!canSubmit || mint.isPending}>
        Mint token
      </Button>
    </>
  );

  return (
    <Modal isOpen={isOpen} onClose={handleClose} title="Mint enrolment token" footer={footer}>
      {error ? (
        <div role="alert" className="mb-3 rounded-tile bg-danger-soft px-3 py-2 text-[13px] text-danger">
          {error}
        </div>
      ) : null}

      {minted ? (
        <div className="flex flex-col gap-3">
          <p className="text-sm text-text-muted">
            This token is shown <span className="font-semibold text-text">only once</span> — copy
            it now. Paste it wherever the shared bootstrap secret used to go: the worker's{' '}
            <code className="text-[12px]">dispatch-token.secret</code> file, or the{' '}
            <code className="text-[12px]">AIZU_WORKER_BOOTSTRAP_TOKEN</code> env var directly. It
            cannot be retrieved again — mint a new one if it's lost.
          </p>
          <div className="flex items-center gap-2 rounded-tile border border-border bg-surface-2 px-3 py-2">
            <input
              readOnly
              value={minted.token}
              aria-label="Enrolment token"
              className="min-w-0 grow bg-transparent text-[12px] text-text-muted outline-none"
              onFocus={(e) => {
                e.currentTarget.select();
              }}
            />
            <Button variant="ghost" onClick={onCopy} aria-label="Copy enrolment token">
              {copied ? <Check className="size-3.5 text-success" aria-hidden /> : <Copy className="size-3.5" aria-hidden />}
              {copied ? 'Copied' : 'Copy'}
            </Button>
          </div>
        </div>
      ) : (
        <>
          <label className="block text-xs font-semibold text-text-muted" htmlFor="wet-scope">
            Scope
          </label>
          <select
            id="wet-scope"
            value={scope}
            onChange={(e) => {
              setScope(e.target.value as EnrolmentScopeKind);
            }}
            className="mt-1 w-full rounded-tile border border-border bg-bg px-3 py-2 text-sm text-text outline-none focus:border-brand"
          >
            <option value="org">Single org</option>
            <option value="pool">Pool (all orgs — shared managed box)</option>
          </select>

          {scope === 'org' ? (
            <>
              <label className="mt-3 block text-xs font-semibold text-text-muted" htmlFor="wet-org">
                Org
              </label>
              <select
                id="wet-org"
                value={orgId}
                onChange={(e) => {
                  setOrgId(e.target.value);
                }}
                className="mt-1 w-full rounded-tile border border-border bg-bg px-3 py-2 text-sm text-text outline-none focus:border-brand"
              >
                <option value="">Select an org…</option>
                {(orgs.data ?? []).map((org) => (
                  <option key={org.id} value={org.id}>
                    {org.name}
                  </option>
                ))}
              </select>
            </>
          ) : null}

          <label className="mt-3 block text-xs font-semibold text-text-muted" htmlFor="wet-label">
            Label (optional)
          </label>
          <input
            id="wet-label"
            value={label}
            onChange={(e) => {
              setLabel(e.target.value);
            }}
            className="mt-1 w-full rounded-tile border border-border bg-bg px-3 py-2 text-sm text-text outline-none focus:border-brand"
          />

          <label className="mt-3 block text-xs font-semibold text-text-muted" htmlFor="wet-ttl">
            Expires after (hours)
          </label>
          <input
            id="wet-ttl"
            inputMode="numeric"
            value={ttlHours}
            onChange={(e) => {
              setTtlHours(e.target.value);
            }}
            className="mt-1 w-full rounded-tile border border-border bg-bg px-3 py-2 text-sm text-text outline-none focus:border-brand"
          />
        </>
      )}
    </Modal>
  );
}
