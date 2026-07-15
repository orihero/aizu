import { useState } from 'react';
import { Flag, Plus } from 'lucide-react';
import { Badge } from '@/shared/ui/Badge';
import { Button } from '@/shared/ui/Button';
import { Card, CardBody, CardHeader } from '@/shared/ui/Card';
import { Modal } from '@/shared/ui/Modal';
import {
  CONTROL_FLAG_SCOPES,
  type ControlFlag,
  type ControlFlagScope,
} from '@/shared/schemas/admin';
import { formatTimestamp } from './format';
import { useSetControlFlag } from './adminHooks';

function FlagRow({ flag }: { readonly flag: ControlFlag }) {
  const setFlag = useSetControlFlag();
  const scopeText = flag.scope === 'global' ? 'global' : `${flag.scope}:${flag.scopeKey}`;
  return (
    <tr className="border-b border-border last:border-0">
      <td className="px-4 py-2.5 font-semibold text-text">{scopeText}</td>
      <td className="px-4 py-2.5">
        <div className="flex flex-wrap gap-1.5">
          {flag.halt ? <Badge tone="danger">halt</Badge> : null}
          {flag.drain ? <Badge tone="warn">drain</Badge> : null}
          {flag.updateRequired ? <Badge tone="info">update</Badge> : null}
        </div>
      </td>
      <td className="px-4 py-2.5 text-text-muted">{flag.reason ?? '—'}</td>
      <td className="px-4 py-2.5 text-xs text-text-faint">{formatTimestamp(flag.updatedAt)}</td>
      <td className="px-4 py-2.5 text-right">
        <Button
          variant="ghost"
          disabled={setFlag.isPending}
          onClick={() => {
            setFlag.mutate({ scope: flag.scope, scopeKey: flag.scopeKey, clear: true });
          }}
        >
          Clear
        </Button>
      </td>
    </tr>
  );
}

function NewFlagModal({ isOpen, onClose }: { readonly isOpen: boolean; readonly onClose: () => void }) {
  const setFlag = useSetControlFlag();
  const [scope, setScope] = useState<ControlFlagScope>('global');
  const [scopeKey, setScopeKey] = useState('');
  const [drain, setDrain] = useState(false);
  const [halt, setHalt] = useState(false);
  const [updateRequired, setUpdateRequired] = useState(false);
  const [reason, setReason] = useState('');
  const [error, setError] = useState<string | null>(null);

  const needsKey = scope !== 'global';
  const canSubmit = (drain || halt || updateRequired) && (!needsKey || scopeKey.trim() !== '');

  function reset() {
    setScope('global');
    setScopeKey('');
    setDrain(false);
    setHalt(false);
    setUpdateRequired(false);
    setReason('');
    setError(null);
  }

  function submit() {
    setError(null);
    setFlag.mutate(
      {
        scope,
        ...(needsKey ? { scopeKey: scopeKey.trim() } : {}),
        drain,
        halt,
        updateRequired,
        reason: reason.trim() || null,
      },
      {
        onSuccess: () => {
          reset();
          onClose();
        },
        onError: (e: unknown) => {
          setError(e instanceof Error ? e.message : 'Failed to set flag');
        },
      },
    );
  }

  const footer = (
    <>
      <Button variant="ghost" onClick={onClose} disabled={setFlag.isPending}>
        Cancel
      </Button>
      <Button onClick={submit} disabled={!canSubmit || setFlag.isPending}>
        Set flag
      </Button>
    </>
  );

  return (
    <Modal isOpen={isOpen} onClose={onClose} title="New control flag" footer={footer}>
      {error ? (
        <div role="alert" className="mb-3 rounded-tile bg-danger-soft px-3 py-2 text-[13px] text-danger">
          {error}
        </div>
      ) : null}
      <label className="block text-xs font-semibold text-text-muted" htmlFor="flag-scope">
        Scope
      </label>
      <select
        id="flag-scope"
        value={scope}
        onChange={(e) => { setScope(e.target.value as ControlFlagScope); }}
        className="mt-1 w-full rounded-tile border border-border bg-bg px-3 py-2 text-sm text-text outline-none focus:border-brand"
      >
        {CONTROL_FLAG_SCOPES.map((s) => (
          <option key={s} value={s}>
            {s}
          </option>
        ))}
      </select>

      {needsKey ? (
        <>
          <label className="mt-3 block text-xs font-semibold text-text-muted" htmlFor="flag-key">
            {scope === 'org' ? 'Org id' : scope === 'platform' ? 'Platform' : 'Worker id'}
          </label>
          <input
            id="flag-key"
            value={scopeKey}
            onChange={(e) => { setScopeKey(e.target.value); }}
            className="mt-1 w-full rounded-tile border border-border bg-bg px-3 py-2 text-sm text-text outline-none focus:border-brand"
          />
        </>
      ) : null}

      <fieldset className="mt-4">
        <legend className="text-xs font-semibold text-text-muted">Directives</legend>
        <div className="mt-2 flex flex-col gap-2 text-sm text-text">
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={halt} onChange={(e) => { setHalt(e.target.checked); }} />
            Halt (tear down + nack running jobs)
          </label>
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={drain} onChange={(e) => { setDrain(e.target.checked); }} />
            Drain (finish current, stop leasing)
          </label>
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={updateRequired}
              onChange={(e) => { setUpdateRequired(e.target.checked); }}
            />
            Update required (force agent update)
          </label>
        </div>
      </fieldset>

      <label className="mt-4 block text-xs font-semibold text-text-muted" htmlFor="flag-reason">
        Reason (optional)
      </label>
      <input
        id="flag-reason"
        value={reason}
        onChange={(e) => { setReason(e.target.value); }}
        className="mt-1 w-full rounded-tile border border-border bg-bg px-3 py-2 text-sm text-text outline-none focus:border-brand"
      />
    </Modal>
  );
}

export function ControlFlagsCard({ flags }: { readonly flags: readonly ControlFlag[] }) {
  const [adding, setAdding] = useState(false);
  return (
    <Card className="mt-6">
      <CardHeader
        title={
          <span className="inline-flex items-center gap-2">
            <Flag className="size-4 text-text-muted" aria-hidden />
            Control flags
          </span>
        }
        subtitle="Drain / halt / update directives, resolved across scopes on each heartbeat."
        actions={
          <Button onClick={() => { setAdding(true); }}>
            <Plus className="size-3.5" aria-hidden />
            New flag
          </Button>
        }
      />
      <CardBody className="px-0 py-0">
        {flags.length === 0 ? (
          <p className="px-5 py-6 text-sm text-text-muted">No control flags set.</p>
        ) : (
          <table className="w-full text-left text-[13px]">
            <thead>
              <tr className="border-b border-border text-xs font-semibold uppercase tracking-wide text-text-faint">
                <th className="px-4 py-2.5">Scope</th>
                <th className="px-4 py-2.5">Directives</th>
                <th className="px-4 py-2.5">Reason</th>
                <th className="px-4 py-2.5">Updated</th>
                <th className="px-4 py-2.5 text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {flags.map((f) => (
                <FlagRow key={`${f.scope}:${f.scopeKey}`} flag={f} />
              ))}
            </tbody>
          </table>
        )}
      </CardBody>
      <NewFlagModal isOpen={adding} onClose={() => { setAdding(false); }} />
    </Card>
  );
}
