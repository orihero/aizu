import { useEffect, useState } from 'react';
import { Sparkles } from 'lucide-react';
import { Button } from '@/shared/ui/Button';
import { Card, CardBody } from '@/shared/ui/Card';

/** Client-side narrative while the AI drafts. Purely cosmetic — the real signal
 *  is the mutation settling; these stages just reassure the user something is
 *  happening. */
const STAGES: readonly string[] = [
  'Reading your link',
  'Analyzing your product',
  'Drafting your campaign',
];

const STAGE_INTERVAL_MS = 1600;

interface CampaignGenerateProgressProps {
  /** Cancel the in-flight generation (the page calls generate.reset()). */
  readonly onCancel: () => void;
}

/**
 * Staged "generating…" panel. The stage advances on a timer and CLAMPS at the
 * last stage — it never loops back, because a looping indicator reads as "stuck".
 * Announced politely for screen readers.
 */
export function CampaignGenerateProgress({ onCancel }: CampaignGenerateProgressProps) {
  const [stage, setStage] = useState(0);

  useEffect(() => {
    // Stop scheduling once we reach the final stage (clamp, don't loop).
    if (stage >= STAGES.length - 1) return;
    const timer = setTimeout(() => { setStage((s) => Math.min(s + 1, STAGES.length - 1)); }, STAGE_INTERVAL_MS);
    return () => { clearTimeout(timer); };
  }, [stage]);

  return (
    <Card>
      <CardBody className="flex flex-col items-center gap-4 p-10 text-center">
        <span className="inline-flex size-12 items-center justify-center rounded-full bg-brand/10 text-brand motion-reduce:animate-none animate-pulse">
          <Sparkles className="size-6" aria-hidden />
        </span>
        <div role="status" aria-live="polite" className="text-sm font-bold text-text">
          {STAGES[stage]}…
        </div>
        <p className="max-w-sm text-[12.5px] text-text-muted">
          This usually takes a few seconds. You can keep this tab open.
        </p>
        <Button variant="ghost" onClick={onCancel}>Cancel</Button>
      </CardBody>
    </Card>
  );
}
