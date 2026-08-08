import { useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { Check, ExternalLink } from 'lucide-react';
import { Card, CardBody, CardHeader } from '@/shared/ui/Card';
import { Button } from '@/shared/ui/Button';
import { Badge, type BadgeTone } from '@/shared/ui/Badge';
import { cn } from '@/shared/lib/cn';
import { formatNumber, formatPercent } from '@/shared/lib/formatters';
import { useOpenBillingPortal, useOpenCheckout } from '@/shared/hooks/useBillingMutations';
import { CheckoutSuccessModal } from './CheckoutSuccessModal';
import type { Billing, BillingInterval, BillingTier } from '@/shared/types/domain';

/** Where the Scale "Talk to sales" CTA points. Single constant so it is easy to
 * change without hunting through JSX. */
const SALES_EMAIL = 'sales@aizu.uz';

/** Subscription status → badge tone. Fails to neutral for any unrecognized status
 * (mirrors the server's closed-set allow-list philosophy — unknown ≠ healthy). */
const STATUS_TONE: Readonly<Record<string, BadgeTone>> = {
  active: 'success',
  trialing: 'info',
  past_due: 'warn',
  unpaid: 'warn',
  incomplete: 'warn',
  paused: 'warn',
  canceled: 'danger',
  revoked: 'danger',
};

const INTERVAL_SUFFIX: Readonly<Record<BillingInterval, string>> = {
  month: '/mo',
  year: '/yr',
};

function statusTone(status: string): BadgeTone {
  return STATUS_TONE[status] ?? 'neutral';
}

function formatPrice(usd: number | null): string {
  if (usd === null) return 'Custom';
  if (usd === 0) return 'Free';
  const value = Number.isInteger(usd) ? formatNumber(usd) : usd.toFixed(2);
  return `$${value}`;
}

/** Unix seconds → a short human date, or null when there is no period (Free). */
function formatPeriodEnd(ts: number | null): string | null {
  if (ts === null) return null;
  return new Date(ts * 1000).toLocaleDateString('en-US', {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
  });
}

/** Leads-used-this-period bar. Turns warn at ~80% (nearLimit) and danger at cap. */
function UsageMeter({ billing }: { readonly billing: Billing }) {
  const { leadsUsed, leadCap, usageRatio, nearLimit } = billing;
  const atLimit = leadsUsed >= leadCap;
  const barTone = atLimit ? 'bg-danger' : nearLimit ? 'bg-warn' : 'bg-accent';
  const width = Math.min(100, Math.round(usageRatio * 100));
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-baseline justify-between text-xs">
        <span className="font-semibold text-text-muted">Leads this period</span>
        <span className="tabular text-text">
          {formatNumber(leadsUsed)} / {formatNumber(leadCap)}{' '}
          <span className="text-text-faint">({formatPercent(usageRatio)})</span>
        </span>
      </div>
      <div className="h-2 overflow-hidden rounded-full bg-surface-2">
        <div className={cn('h-full rounded-full transition-all', barTone)} style={{ width: `${width}%` }} />
      </div>
      {atLimit ? (
        <p className="text-[11px] font-medium text-danger">
          Plan limit reached — new runs are blocked until the period resets or you upgrade.
        </p>
      ) : nearLimit ? (
        <p className="text-[11px] font-medium text-warn">
          You&apos;re close to your plan limit. Consider upgrading to avoid interruptions.
        </p>
      ) : null}
    </div>
  );
}

/** Current plan + status + renewal + usage + Manage-billing. */
function PlanSummaryCard({ billing }: { readonly billing: Billing }) {
  const portal = useOpenBillingPortal();
  const current = billing.tiers.find((t) => t.tier === billing.tier);
  const planName = current?.displayName ?? billing.tier;
  const renewal = formatPeriodEnd(billing.periodEnd);
  const suffix = billing.interval ? INTERVAL_SUFFIX[billing.interval as BillingInterval] : '';
  const price = current ? formatPrice(current.prices[(billing.interval ?? 'month') as BillingInterval]) : null;
  // A Free org has no Polar customer; the portal degrades to a "no account" message.
  // isSuccess narrows `data` to the resolved BillingPortal (no optional chain needed).
  const noAccount = portal.isSuccess && !portal.data.hasAccount;

  return (
    <Card>
      <CardHeader
        title="Current plan"
        subtitle="Your subscription, renewal and usage."
        actions={
          <Button variant="ghost" onClick={() => { portal.mutate(); }} disabled={portal.isPending}>
            {portal.isPending ? 'Opening…' : 'Manage billing'}
            <ExternalLink className="size-3.5" aria-hidden />
          </Button>
        }
      />
      <CardBody className="flex flex-col gap-5">
        <div className="flex flex-wrap items-center gap-3">
          <span className="text-lg font-bold text-text">{planName}</span>
          <Badge tone={statusTone(billing.status)}>{billing.status}</Badge>
          {price && billing.tier !== 'free' ? (
            <span className="text-sm text-text-muted">
              {price}
              <span className="text-text-faint">{suffix}</span>
            </span>
          ) : null}
          {billing.cancelAtPeriodEnd ? (
            <Badge tone="warn" title="This subscription will not renew.">Cancels at period end</Badge>
          ) : null}
        </div>
        {renewal ? (
          <p className="text-xs text-text-muted">
            {billing.cancelAtPeriodEnd ? 'Access ends' : 'Renews'}{' '}
            <span className="font-semibold text-text">{renewal}</span>
          </p>
        ) : null}
        <UsageMeter billing={billing} />
        {noAccount ? (
          <p className="text-[11px] text-text-faint">
            No billing account yet — upgrade to a paid plan to manage billing.
          </p>
        ) : portal.isError ? (
          <p className="text-[11px] font-medium text-danger">
            {portal.error instanceof Error ? portal.error.message : 'Could not open billing.'}
          </p>
        ) : null}
      </CardBody>
    </Card>
  );
}

/** One tier column in the comparison grid. */
function TierCard({
  tier,
  interval,
  isCurrent,
  hasActivePaidPlan,
  onUpgrade,
  onManage,
  pending,
}: {
  readonly tier: BillingTier;
  readonly interval: BillingInterval;
  readonly isCurrent: boolean;
  // True when the org already has an active paid subscription. A checkout can
  // only CREATE a subscription — Polar rejects it for an existing subscriber —
  // so plan changes must go through the customer portal, not checkout.
  readonly hasActivePaidPlan: boolean;
  readonly onUpgrade: () => void;
  readonly onManage: () => void;
  readonly pending: boolean;
}) {
  const price = formatPrice(tier.prices[interval]);
  const isFree = tier.tier === 'free';
  // selfServe=false AND not free → the sales-led Scale tier.
  const isSalesLed = !tier.selfServe && !isFree;

  return (
    <Card className={cn('flex flex-col', isCurrent && 'ring-2 ring-accent')}>
      <CardBody className="flex flex-1 flex-col gap-3">
        <div className="flex items-center justify-between">
          <span className="text-sm font-bold text-text">{tier.displayName}</span>
          {isCurrent ? <Badge tone="info">Current</Badge> : null}
        </div>
        <div>
          <span className="text-xl font-bold text-text">{price}</span>
          {!isFree && !isSalesLed ? (
            <span className="text-xs text-text-faint">{INTERVAL_SUFFIX[interval]}</span>
          ) : null}
        </div>
        <p className="text-xs text-text-muted">
          {isSalesLed ? 'Custom lead volume' : `${formatNumber(tier.leadCap)} leads / period`}
        </p>
        <div className="mt-auto pt-2">
          {isCurrent ? (
            <span className="inline-flex items-center gap-1 text-xs font-semibold text-accent">
              <Check className="size-3.5" aria-hidden /> Your plan
            </span>
          ) : isFree ? (
            <span className="text-xs text-text-faint">Default plan</span>
          ) : isSalesLed ? (
            <a
              href={`mailto:${SALES_EMAIL}?subject=AIZU%20Scale%20plan`}
              className="inline-flex items-center gap-1.5 rounded-full border border-border bg-surface px-3.5 py-1.5 text-xs font-bold text-text shadow-tile transition hover:shadow-lift"
            >
              Talk to sales
            </a>
          ) : hasActivePaidPlan ? (
            <Button onClick={onManage} disabled={pending} aria-label={`Change plan to ${tier.displayName}`}>
              {pending ? 'Opening…' : 'Change plan'}
            </Button>
          ) : (
            <Button onClick={onUpgrade} disabled={pending} aria-label={`Upgrade to ${tier.displayName}`}>
              {pending ? 'Redirecting…' : 'Upgrade'}
            </Button>
          )}
        </div>
      </CardBody>
    </Card>
  );
}

/** month⇄year segmented toggle. Annual advertises "~2 months free". */
function IntervalToggle({
  interval,
  onChange,
}: {
  readonly interval: BillingInterval;
  readonly onChange: (next: BillingInterval) => void;
}) {
  const options: readonly { readonly key: BillingInterval; readonly label: string }[] = [
    { key: 'month', label: 'Monthly' },
    { key: 'year', label: 'Annual' },
  ];
  return (
    <div className="inline-flex items-center gap-2">
      <div className="inline-flex rounded-full border border-border bg-surface p-0.5">
        {options.map((opt) => (
          <button
            key={opt.key}
            type="button"
            aria-pressed={interval === opt.key}
            onClick={() => { onChange(opt.key); }}
            className={cn(
              'rounded-full px-3 py-1 text-xs font-bold transition',
              interval === opt.key ? 'bg-accent text-accent-ink' : 'text-text-muted hover:text-text',
            )}
          >
            {opt.label}
          </button>
        ))}
      </div>
      {interval === 'year' ? <span className="text-[11px] font-medium text-success">~2 months free</span> : null}
    </div>
  );
}

export function BillingPanel({ billing }: { readonly billing: Billing }) {
  const [interval, setInterval] = useState<BillingInterval>(
    (billing.interval as BillingInterval | null) ?? 'month',
  );
  const checkout = useOpenCheckout();
  const portal = useOpenBillingPortal();
  // A checkout can only create a NEW subscription, which Polar rejects once one
  // is active. Existing paid subscribers change plans via the portal instead.
  const hasActivePaidPlan =
    billing.tier !== 'free' && (billing.status === 'active' || billing.status === 'trialing');

  // Polar redirects back here with ?checkout=success after a completed payment.
  // Show the celebration + plan-summary modal once, then strip the marker so a
  // refresh (or navigating back) does not re-open it.
  const [searchParams, setSearchParams] = useSearchParams();
  const showCheckoutSuccess = searchParams.get('checkout') === 'success';
  const dismissCheckoutSuccess = () => {
    const next = new URLSearchParams(searchParams);
    next.delete('checkout');
    setSearchParams(next, { replace: true });
  };

  return (
    <div className="flex flex-col gap-5">
      <CheckoutSuccessModal
        billing={billing}
        isOpen={showCheckoutSuccess}
        onClose={dismissCheckoutSuccess}
      />
      <PlanSummaryCard billing={billing} />
      <Card>
        <CardHeader
          title="Plans"
          subtitle="Upgrade or change your plan. Annual billing saves ~2 months."
          actions={<IntervalToggle interval={interval} onChange={setInterval} />}
        />
        <CardBody className="flex flex-col gap-4">
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
            {billing.tiers.map((tier) => (
              <TierCard
                key={tier.tier}
                tier={tier}
                interval={interval}
                isCurrent={tier.tier === billing.tier}
                hasActivePaidPlan={hasActivePaidPlan}
                pending={
                  hasActivePaidPlan
                    ? portal.isPending
                    : checkout.isPending && checkout.variables.tier === tier.tier
                }
                onUpgrade={() => { checkout.mutate({ tier: tier.tier, interval }); }}
                onManage={() => { portal.mutate(); }}
              />
            ))}
          </div>
          {checkout.isError ? (
            <p className="text-xs font-medium text-danger">
              {checkout.error instanceof Error ? checkout.error.message : 'Could not start checkout.'}
            </p>
          ) : null}
          {portal.isError ? (
            <p className="text-xs font-medium text-danger">
              {portal.error instanceof Error ? portal.error.message : 'Could not open the billing portal.'}
            </p>
          ) : null}
        </CardBody>
      </Card>
    </div>
  );
}
