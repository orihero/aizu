import { useEffect, useRef } from 'react';
import { Sparkles } from 'lucide-react';
import { Button } from '@/shared/ui/Button';
import { Card, CardBody } from '@/shared/ui/Card';
import { ImageDropzone } from '@/shared/ui/ImageDropzone';
import { ResultError, type AppError } from '@/shared/lib/result';
import { useRunInterview } from '@/shared/hooks/useWriteMutations';
import type { InterviewResponse } from '@/shared/types/domain';
import { FIELD_CLASS, LABEL_CLASS } from './formStyles';
import { describeGenerateError } from './describeGenerateError';
import { CampaignGenerateProgress } from './CampaignGenerateProgress';

/** The composer's inputs live in the wizard so "Regenerate" can return here with
 *  them intact. The composer is a controlled view over that state. */
export interface ComposerInputs {
  readonly text: string;
  readonly url: string;
  readonly imageB64: string | null;
}

interface CampaignComposerProps {
  readonly inputs: ComposerInputs;
  readonly onInputsChange: (patch: Partial<ComposerInputs>) => void;
  /** Ran the first interview round → the wizard takes over (questions or draft). */
  readonly onStarted: (response: InterviewResponse) => void;
  /** Skip AI and build the campaign by hand. */
  readonly onSkip: () => void;
}

/** Extracts the typed AppError a failed generate mutation threw. */
function toAppError(error: unknown): AppError {
  if (error instanceof ResultError) return error.appError;
  return { kind: 'unknown', message: error instanceof Error ? error.message : 'Generation failed.' };
}

/**
 * Step 1 of the AI-first wizard: describe the product (text / URL / screenshot),
 * generate a draft, then review. At least one input is required before the CTA
 * enables. While generating, the page swaps in CampaignGenerateProgress; this
 * component owns the error panel for a failed attempt.
 */
export function CampaignComposer({ inputs, onInputsChange, onStarted, onSkip }: CampaignComposerProps) {
  const interview = useRunInterview();
  const headingRef = useRef<HTMLHeadingElement>(null);

  // Move focus to the composer heading on mount / when returning from review,
  // so keyboard + screen-reader users land at the top of the step.
  useEffect(() => { headingRef.current?.focus(); }, []);

  const hasInput =
    inputs.text.trim().length > 0 || inputs.url.trim().length > 0 || inputs.imageB64 !== null;

  function submit() {
    if (!hasInput || interview.isPending) return;
    interview.mutate(
      {
        ...(inputs.text.trim() ? { text: inputs.text.trim() } : {}),
        ...(inputs.url.trim() ? { url: inputs.url.trim() } : {}),
        ...(inputs.imageB64 ? { imageB64: inputs.imageB64 } : {}),
        round: 1,
      },
      { onSuccess: (response) => { onStarted(response); } },
    );
  }

  // While the request is in flight, show the staged progress in place. Cancel
  // resets the (transient) mutation, returning to the form with inputs intact.
  if (interview.isPending) {
    return <CampaignGenerateProgress onCancel={() => { interview.reset(); }} />;
  }

  const failure = interview.isError ? describeGenerateError(toAppError(interview.error)) : null;

  return (
    <Card>
      <CardBody className="flex flex-col gap-6 p-6">
        <div>
          <h2
            ref={headingRef}
            tabIndex={-1}
            className="text-lg font-extrabold text-text outline-none"
          >
            Describe your product
          </h2>
          <p className="mt-1 text-[13px] text-text-muted">
            Add a description, a landing-page link, or a screenshot — we’ll ask a few quick
            questions, then draft a campaign you can tweak.
          </p>
        </div>

        <div>
          <label htmlFor="composer-text" className={LABEL_CLASS}>What are you promoting?</label>
          <textarea
            id="composer-text"
            rows={4}
            value={inputs.text}
            onChange={(e) => { onInputsChange({ text: e.target.value }); }}
            placeholder="e.g. A team project-management SaaS, targeting startups evaluating new tools…"
            className={FIELD_CLASS}
          />
        </div>

        <div>
          <label htmlFor="composer-url" className={LABEL_CLASS}>Landing page or profile (optional)</label>
          <input
            id="composer-url"
            type="url"
            value={inputs.url}
            onChange={(e) => { onInputsChange({ url: e.target.value }); }}
            placeholder="https://acme.example.com"
            autoComplete="off"
            className={FIELD_CLASS}
          />
        </div>

        <div>
          <span className={LABEL_CLASS}>Product screenshot (optional)</span>
          <ImageDropzone
            value={inputs.imageB64}
            onChange={(dataUrl) => { onInputsChange({ imageB64: dataUrl }); }}
          />
        </div>

        {failure ? (
          <div role="alert" className="rounded-tile border border-danger/40 bg-danger/5 px-3.5 py-3">
            <p className="text-[13px] font-bold text-danger">{failure.title}</p>
            <p className="mt-0.5 text-[12px] text-text-muted">{failure.hint}</p>
            <div className="mt-3 flex flex-wrap items-center gap-2">
              <Button onClick={submit} disabled={!hasInput}>Try again</Button>
              {failure.canFillManually ? (
                <Button variant="ghost" onClick={onSkip}>Fill it in manually</Button>
              ) : null}
            </div>
          </div>
        ) : null}

        <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border pt-4">
          <button
            type="button"
            onClick={onSkip}
            className="text-[13px] font-bold text-text-muted underline-offset-2 transition hover:text-text hover:underline"
          >
            Start from scratch
          </button>
          <Button onClick={submit} disabled={!hasInput || interview.isPending}>
            <Sparkles className="size-4" aria-hidden />
            Continue
          </Button>
        </div>
      </CardBody>
    </Card>
  );
}
