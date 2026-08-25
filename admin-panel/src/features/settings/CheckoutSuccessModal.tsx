import { useNavigate } from 'react-router-dom';
import { PartyPopper, Check } from 'lucide-react';
import { Modal } from '@/shared/ui/Modal';
import { Button } from '@/shared/ui/Button';
import { Confetti } from '@/shared/ui/Confetti';
import { formatNumber } from '@/shared/lib/formatters';
import type { Billing, BillingInterval } from '@/shared/types/domain';

const INTERVAL_LABEL: Record<BillingInterval, string> = { month: 'monthly', year: 'annual' };

/**
 * Post-checkout celebration: confetti + the plan the org just activated and its
 * limits. Shown when the billing page is opened with ?checkout=success. The CTA
 * takes the user into the app (home); closing (X/Escape/backdrop) just dismisses.
 */
export function CheckoutSuccessModal({
  billing,
  isOpen,
  onClose,
}: {
  readonly billing: Billing;
  readonly isOpen: boolean;
  readonly onClose: () => void;
}) {
  const navigate = useNavigate();
  const current = billing.tiers.find((t) => t.tier === billing.tier);
  const planName = current?.displayName ?? billing.tier;
  const intervalLabel = billing.interval ? INTERVAL_LABEL[billing.interval as BillingInterval] : null;

  const goHome = () => {
    onClose();
    void navigate('/', { replace: true });
  };

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title="Subscription active"
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>Stay on billing</Button>
          <Button onClick={goHome}>Go to dashboard</Button>
        </>
      }
    >
      <Confetti />
      <div className="relative flex flex-col items-center gap-3 py-2 text-center">
        <div className="flex size-12 items-center justify-center rounded-full bg-success-soft text-success">
          <PartyPopper className="size-6" aria-hidden />
        </div>
        <div>
          <p className="text-lg font-extrabold tracking-tight text-text">You’re on {planName}! 🎉</p>
          <p className="mt-0.5 text-[13px] text-text-muted">
            Payment went through — your plan is now active
            {intervalLabel ? ` (${intervalLabel} billing)` : ''}.
          </p>
        </div>
        <ul className="mt-1 w-full space-y-1.5 rounded-tile border border-border bg-surface-2 px-4 py-3 text-left text-[13px]">
          <li className="flex items-center gap-2 text-text">
            <Check className="size-4 shrink-0 text-success" aria-hidden />
            Up to <span className="font-bold">{formatNumber(billing.leadCap)}</span> leads per billing period
          </li>
          <li className="flex items-center gap-2 text-text">
            <Check className="size-4 shrink-0 text-success" aria-hidden />
            You’ve used {formatNumber(billing.leadsUsed)} of {formatNumber(billing.leadCap)} this period
          </li>
          {/* The second period allowance (v27 Section F). Omitted when the cap is null —
              unlimited, and also what a bridge predating the reveal cap sends, neither of
              which is worth a line that would read "up to 0". */}
          {billing.revealCap !== null ? (
            <li className="flex items-center gap-2 text-text">
              <Check className="size-4 shrink-0 text-success" aria-hidden />
              Reveal who’s behind up to{' '}
              <span className="font-bold">{formatNumber(billing.revealCap)}</span> of them —
              you’ve revealed {formatNumber(billing.revealsUsed)} this period
            </li>
          ) : null}
          <li className="flex items-center gap-2 text-text-muted">
            <Check className="size-4 shrink-0 text-success" aria-hidden />
            Manage or change your plan anytime from Billing
          </li>
        </ul>
      </div>
    </Modal>
  );
}
