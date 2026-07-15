import { useState } from 'react';
import { ArrowLeft } from 'lucide-react';
import { Link } from 'react-router-dom';
import { PageHeader } from '@/app/layout/PageHeader';
import { Button } from '@/shared/ui/Button';
import { Card, CardBody } from '@/shared/ui/Card';
import { ResultError, type AppError } from '@/shared/lib/result';
import { useGenerateCampaign, useRunInterview } from '@/shared/hooks/useWriteMutations';
import type { InterviewAnswer, InterviewResponse } from '@/shared/types/domain';
import { CampaignComposer, type ComposerInputs } from './CampaignComposer';
import { CampaignInterview, type InterviewRoundResult } from './CampaignInterview';
import { CampaignGenerateProgress } from './CampaignGenerateProgress';
import { CampaignReview } from './CampaignReview';
import { describeGenerateError } from './describeGenerateError';
import type { CampaignDraft } from './useCampaignForm';

const BACK_LINK_CLASS =
  'inline-flex items-center gap-1.5 rounded-full border border-border bg-surface px-3.5 py-1.5 text-xs font-bold text-text shadow-tile transition hover:-translate-y-px hover:shadow-lift active:scale-95';

const EMPTY_INPUTS: ComposerInputs = { text: '', url: '', imageB64: null };

/**
 * The wizard's coarse step. Mutation-pending moments (running the next round,
 * synthesizing) are rendered from the live mutation state, not separate steps.
 */
type WizardStep =
  | { readonly step: 'intro' }
  | { readonly step: 'interview'; readonly response: InterviewResponse }
  | { readonly step: 'review'; readonly draft: CampaignDraft };

/** Extracts the typed AppError a failed mutation threw. */
function toAppError(error: unknown): AppError {
  if (error instanceof ResultError) return error.appError;
  return { kind: 'unknown', message: error instanceof Error ? error.message : 'Something went wrong.' };
}

/**
 * AI-first new-campaign wizard. Step 1 (intro) collects the product signal and
 * launches a conversational interview; the AI asks rounds of clarifying questions
 * (the composer runs round 1, this page runs the rest) until it's confident, then
 * all answers + context are sent back to synthesize a refined brief that prefills
 * the standard create form (review). The transcript/context/platforms live here so
 * each round resends them; the form persists through the unchanged create path.
 */
export function CampaignNewPage() {
  const [wizard, setWizard] = useState<WizardStep>({ step: 'intro' });
  const [inputs, setInputs] = useState<ComposerInputs>(EMPTY_INPUTS);
  // Carried across rounds: the serialized product context (built once), the
  // running Q&A transcript, and the platforms the client chose.
  const [productContext, setProductContext] = useState('');
  const [history, setHistory] = useState<readonly InterviewAnswer[]>([]);
  const [platforms, setPlatforms] = useState<readonly string[] | null>(null);

  const interview = useRunInterview();
  const generate = useGenerateCampaign();

  function reset() {
    setProductContext('');
    setHistory([]);
    setPlatforms(null);
    interview.reset();
    generate.reset();
  }

  /** Send context + transcript + platforms to synthesize the brief, then review. */
  function synthesize(context: string, transcript: readonly InterviewAnswer[], picked: readonly string[] | null) {
    generate.mutate(
      {
        ...(inputs.text.trim() ? { text: inputs.text.trim() } : {}),
        ...(context ? { productContext: context } : {}),
        ...(transcript.length ? { interview: [...transcript] } : {}),
        ...(picked && picked.length ? { platforms: [...picked] } : {}),
      },
      { onSuccess: (draft) => { setWizard({ step: 'review', draft }); } },
    );
  }

  /** A fresh interview response (round 1 from the composer, or a later round):
   *  draft now if the AI is done / out of questions, else show the questions. */
  function consume(response: InterviewResponse, transcript: readonly InterviewAnswer[], picked: readonly string[] | null) {
    const context = response.productContext || productContext;
    setProductContext(context);
    if (response.done || response.questions.length === 0) {
      setWizard({ step: 'interview', response });   // unmount composer; progress shows while pending
      synthesize(context, transcript, picked);
    } else {
      setWizard({ step: 'interview', response });
    }
  }

  function onRoundContinue(prev: InterviewResponse, result: InterviewRoundResult) {
    const nextHistory = [...history, ...result.answers];
    const picked = result.platforms ?? platforms;
    setHistory(nextHistory);
    if (result.platforms) setPlatforms(result.platforms);
    interview.mutate(
      { productContext, interview: nextHistory, round: prev.round + 1 },
      { onSuccess: (response) => { consume(response, nextHistory, picked); } },
    );
  }

  function onRoundFinish(result: InterviewRoundResult) {
    const nextHistory = [...history, ...result.answers];
    const picked = result.platforms ?? platforms;
    setHistory(nextHistory);
    if (result.platforms) setPlatforms(result.platforms);
    synthesize(productContext, nextHistory, picked);
  }

  return (
    <>
      <PageHeader
        title="New campaign"
        subtitle="Describe your product, answer a few questions, and let AI draft a campaign — or build one by hand."
        actions={
          <Link to="/campaigns" className={BACK_LINK_CLASS}>
            <ArrowLeft className="size-4" aria-hidden />
            Back to campaigns
          </Link>
        }
      />
      {renderBody()}
    </>
  );

  function renderBody() {
    // Synthesizing the final brief: full-page progress regardless of step.
    if (generate.isPending) {
      return <CampaignGenerateProgress onCancel={() => { generate.reset(); }} />;
    }
    if (generate.isError) {
      return <WizardError error={generate.error} onRetry={() => { synthesize(productContext, history, platforms); }} onManual={goManual} />;
    }

    if (wizard.step === 'review') {
      return <CampaignReview draft={wizard.draft} onRegenerate={goToCompose} />;
    }

    if (wizard.step === 'interview') {
      if (interview.isPending) {
        return <CampaignGenerateProgress onCancel={() => { interview.reset(); }} />;
      }
      if (interview.isError) {
        const prev = wizard.response;
        return (
          <WizardError
            error={interview.error}
            onRetry={() => { interview.mutate({ productContext, interview: [...history], round: prev.round + 1 }, { onSuccess: (r) => { consume(r, history, platforms); } }); }}
            onManual={goManual}
          />
        );
      }
      return (
        <CampaignInterview
          key={wizard.response.round}
          questions={wizard.response.questions}
          round={wizard.response.round}
          onContinue={(result) => { onRoundContinue(wizard.response, result); }}
          onFinish={onRoundFinish}
        />
      );
    }

    return (
      <CampaignComposer
        inputs={inputs}
        onInputsChange={(patch) => { setInputs((prev) => ({ ...prev, ...patch })); }}
        onStarted={(response) => { consume(response, [], null); }}
        onSkip={goManual}
      />
    );
  }

  function goManual() {
    reset();
    setWizard({ step: 'review', draft: {} });
  }

  function goToCompose() {
    reset();
    setWizard({ step: 'intro' });
  }
}

interface WizardErrorProps {
  readonly error: unknown;
  readonly onRetry: () => void;
  readonly onManual: () => void;
}

/** A round/synthesis failure panel (mirrors the composer's error affordances). */
function WizardError({ error, onRetry, onManual }: WizardErrorProps) {
  const failure = describeGenerateError(toAppError(error));
  return (
    <Card>
      <CardBody className="flex flex-col gap-3 p-6">
        <div role="alert" className="rounded-tile border border-danger/40 bg-danger/5 px-3.5 py-3">
          <p className="text-[13px] font-bold text-danger">{failure.title}</p>
          <p className="mt-0.5 text-[12px] text-text-muted">{failure.hint}</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button onClick={onRetry}>Try again</Button>
          <Button variant="ghost" onClick={onManual}>Fill it in manually</Button>
        </div>
      </CardBody>
    </Card>
  );
}
