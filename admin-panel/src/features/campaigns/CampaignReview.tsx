import { useEffect, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { RotateCw, Sparkles } from 'lucide-react';
import { useCreateCampaign } from '@/shared/hooks/useWriteMutations';
import { CampaignForm } from './CampaignForm';
import { useCampaignForm, type CampaignDraft } from './useCampaignForm';

const DRAFT_NOTE =
  'New campaigns start as a draft — the engine needs a brief file (config/campaign.md) to actually run.';

interface CampaignReviewProps {
  /** The AI-drafted prefill, or {} for the manual (start-from-scratch) path. */
  readonly draft: CampaignDraft;
  /** Return to the composer to draft again (only shown for an AI draft). */
  readonly onRegenerate: () => void;
}

/**
 * Step 2: the standard create form, prefilled with the AI draft. Mounted ONLY
 * once the draft exists — useCampaignForm reads its initializer once, so the
 * draft must be present on this component's first render (the page mounts this
 * in the `review` branch). The "Drafted by AI" banner is suppressed on the
 * manual path (empty draft).
 */
export function CampaignReview({ draft, onRegenerate }: CampaignReviewProps) {
  const navigate = useNavigate();
  const createCampaign = useCreateCampaign();
  const api = useCampaignForm(undefined, draft);
  const bannerRef = useRef<HTMLDivElement>(null);

  const isAiDraft = Object.keys(draft).length > 0;

  // On entering review, move focus to the banner (AI path) so the transition is
  // announced and keyboard users land at the top of the new step.
  useEffect(() => { bannerRef.current?.focus(); }, []);

  return (
    <div className="mx-auto flex max-w-2xl flex-col gap-4">
      {isAiDraft ? (
        <div
          ref={bannerRef}
          tabIndex={-1}
          className="flex flex-wrap items-center justify-between gap-3 rounded-tile border border-border bg-surface-2 px-3.5 py-3 outline-none"
        >
          <p className="flex items-start gap-2 text-xs font-semibold text-text-muted">
            <Sparkles className="mt-0.5 size-4 shrink-0 text-brand" aria-hidden />
            Drafted by AI — review and tweak before saving
          </p>
          <button
            type="button"
            onClick={onRegenerate}
            className="inline-flex items-center gap-1.5 rounded-full border border-border bg-surface px-3 py-1.5 text-[11.5px] font-bold text-text shadow-tile transition hover:-translate-y-px hover:shadow-lift active:scale-95"
          >
            <RotateCw className="size-3.5" aria-hidden />
            Regenerate
          </button>
        </div>
      ) : null}
      <CampaignForm
        api={api}
        onSubmit={() => {
          createCampaign.mutate(api.toInput(), {
            onSuccess: () => { void navigate('/campaigns'); },
          });
        }}
        isPending={createCampaign.isPending}
        isError={createCampaign.isError}
        errorMessage={
          createCampaign.error instanceof Error ? createCampaign.error.message : undefined
        }
        submitLabel="Save draft"
        note={DRAFT_NOTE}
      />
    </div>
  );
}
