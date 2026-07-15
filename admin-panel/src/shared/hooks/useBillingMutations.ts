import { useMutation } from '@tanstack/react-query';
import { usePanelRepository } from '@/shared/api/repositoryContext';
import { unwrap } from '@/shared/lib/result';
import type { CheckoutInput } from '@/shared/types/domain';

/**
 * Billing mutations. Both open a Polar-hosted page, so on success we redirect the
 * browser via `window.location.assign(url)` — there is no local state to invalidate
 * (the org's subscription flips server-side via the webhook, and the next
 * `/api/settings` fetch after the user returns reflects it).
 */

/** Start a self-serve checkout (lite/starter/pro) and redirect to Polar's hosted page. */
export function useOpenCheckout() {
  const repository = usePanelRepository();
  return useMutation({
    mutationFn: async (input: CheckoutInput) => {
      const { checkoutUrl } = unwrap(await repository.openCheckout(input));
      window.location.assign(checkoutUrl);
    },
  });
}

/** Open the Polar customer portal. A Free org with no Polar customer yet resolves
 * with `hasAccount: false` — the caller surfaces that instead of a dead redirect. */
export function useOpenBillingPortal() {
  const repository = usePanelRepository();
  return useMutation({
    mutationFn: async () => {
      const result = unwrap(await repository.openBillingPortal());
      if (result.hasAccount) window.location.assign(result.portalUrl);
      return result;
    },
  });
}
