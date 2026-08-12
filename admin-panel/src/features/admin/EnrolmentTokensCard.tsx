import { useState } from 'react';
import { KeyRound, Plus, ShieldOff } from 'lucide-react';
import { AsyncBoundary } from '@/shared/ui/AsyncBoundary';
import { Badge } from '@/shared/ui/Badge';
import { Button } from '@/shared/ui/Button';
import { Card, CardBody, CardHeader } from '@/shared/ui/Card';
import { ConfirmDialog } from '@/shared/ui/ConfirmDialog';
import { EmptyState } from '@/shared/ui/EmptyState';
import type { EnrolmentToken } from '@/shared/schemas/admin';
import { formatTimestamp } from './format';
import { useEnrolmentTokens, useRevokeEnrolmentToken } from './adminHooks';
import { MintEnrolmentTokenModal } from './MintEnrolmentTokenModal';

type TokenStatus = 'pending' | 'redeemed' | 'revoked';

function tokenStatus(token: EnrolmentToken): TokenStatus {
  if (token.redeemedAt !== null) return 'redeemed';
  if (token.revokedAt !== null) return 'revoked';
  return 'pending';
}

function scopeLabel(token: EnrolmentToken): string {
  return token.scopeKind === 'org' ? `Org #${token.orgId ?? '?'}` : 'Pool (all orgs)';
}

function TokenRow({ token }: { readonly token: EnrolmentToken }) {
  const revoke = useRevokeEnrolmentToken();
  const [confirming, setConfirming] = useState(false);
  const status = tokenStatus(token);
  const label = token.label ?? token.id;

  return (
    <tr className="border-b border-border last:border-0">
      <td className="px-4 py-2.5 font-semibold text-text" title={token.id}>
        {label}
      </td>
      <td className="px-4 py-2.5 text-text-muted">{scopeLabel(token)}</td>
      <td className="px-4 py-2.5 text-xs text-text-faint">{formatTimestamp(token.createdAt)}</td>
      <td className="px-4 py-2.5 text-xs text-text-faint">{formatTimestamp(token.expiresAt)}</td>
      <td className="px-4 py-2.5">
        {status === 'pending' ? <Badge tone="info">pending</Badge> : null}
        {status === 'redeemed' ? (
          <Badge tone="success" title={token.redeemedByWorkerId ?? 'unknown worker'}>
            redeemed &middot; {token.redeemedByWorkerId ?? 'unknown worker'}
          </Badge>
        ) : null}
        {status === 'revoked' ? <Badge tone="danger">revoked</Badge> : null}
      </td>
      <td className="px-4 py-2.5 text-right">
        {status === 'pending' ? (
          <Button variant="ghost" onClick={() => { setConfirming(true); }}>
            <ShieldOff className="size-3.5" aria-hidden />
            Revoke
          </Button>
        ) : status === 'redeemed' ? (
          <span className="text-xs text-text-faint">revoke the worker instead</span>
        ) : (
          <span className="text-xs text-text-faint">&mdash;</span>
        )}
        <ConfirmDialog
          isOpen={confirming}
          title="Revoke enrolment token?"
          tone="danger"
          confirmLabel="Revoke"
          isPending={revoke.isPending}
          message={
            <>
              <span className="font-semibold text-text">{label}</span> will no longer be
              redeemable by any worker. This does not affect a worker already enrolled with it.
            </>
          }
          onConfirm={() => {
            revoke.mutate(token.id, { onSettled: () => { setConfirming(false); } });
          }}
          onClose={() => { setConfirming(false); }}
        />
      </td>
    </tr>
  );
}

/**
 * Mint / list / revoke per-worker enrolment tokens (v22, BUILD-PLAN B8 fix). A
 * worker's org scope is now a SERVER-ASSIGNED, admin-recorded decision made here
 * at mint time, rather than something the box self-declares at register.
 */
export function EnrolmentTokensCard() {
  const tokens = useEnrolmentTokens();
  const [minting, setMinting] = useState(false);
  const rows = tokens.data ?? [];

  return (
    <Card className="mt-6">
      <CardHeader
        title={
          <span className="inline-flex items-center gap-2">
            <KeyRound className="size-4 text-text-muted" aria-hidden />
            Enrolment tokens
          </span>
        }
        subtitle="Single-use, per-worker tokens that assign a box's org/pool scope at first register."
        actions={
          <Button onClick={() => { setMinting(true); }}>
            <Plus className="size-3.5" aria-hidden />
            Mint token
          </Button>
        }
      />
      <CardBody className="px-0 py-0">
        <AsyncBoundary
          isLoading={tokens.isLoading}
          error={tokens.error}
          onRetry={() => {
            void tokens.refetch();
          }}
        >
          {rows.length === 0 ? (
            <EmptyState
              icon={KeyRound}
              title="No enrolment tokens minted yet"
              description="Mint a token to enroll a new worker box with a server-assigned org or pool scope."
            />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[720px] text-left text-[13px]">
                <thead>
                  <tr className="border-b border-border text-xs font-semibold uppercase tracking-wide text-text-faint">
                    <th className="px-4 py-2.5">Token</th>
                    <th className="px-4 py-2.5">Scope</th>
                    <th className="px-4 py-2.5">Created</th>
                    <th className="px-4 py-2.5">Expires</th>
                    <th className="px-4 py-2.5">Status</th>
                    <th className="px-4 py-2.5 text-right">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((t) => (
                    <TokenRow key={t.id} token={t} />
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </AsyncBoundary>
      </CardBody>
      <MintEnrolmentTokenModal isOpen={minting} onClose={() => { setMinting(false); }} />
    </Card>
  );
}
