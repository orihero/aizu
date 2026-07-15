import { Check, Plus } from 'lucide-react';
import { platformLabel } from '@/shared/lib/platformLabel';
import { PLATFORMS } from './useCampaignForm';

interface PlatformPickerQuestionProps {
  /** The AI's recommended platforms (rendered with a "Suggested" badge). */
  readonly suggested: readonly string[];
  /** Currently selected platform slugs. */
  readonly selected: readonly string[];
  readonly onChange: (next: readonly string[]) => void;
}

function chipClass(isSelected: boolean): string {
  return [
    'inline-flex items-center gap-1.5 rounded-full border px-3.5 py-1.5 text-[13px] font-bold transition active:scale-95',
    isSelected
      ? 'border-brand bg-brand/10 text-brand'
      : 'border-border bg-surface text-text-muted hover:border-brand/40 hover:text-text',
  ].join(' ');
}

/**
 * The platforms step of the interview: every supported platform is a toggle chip,
 * with the AI's recommendations pre-selected and badged "Suggested". The user adds
 * or removes any. Chosen slugs flow into the campaign's primary platform + channels.
 */
export function PlatformPickerQuestion({ suggested, selected, onChange }: PlatformPickerQuestionProps) {
  function toggle(platform: string) {
    onChange(
      selected.includes(platform)
        ? selected.filter((p) => p !== platform)
        : [...selected, platform],
    );
  }

  return (
    <div className="flex flex-wrap gap-2">
      {PLATFORMS.map((platform) => {
        const isSelected = selected.includes(platform);
        const isSuggested = suggested.includes(platform);
        return (
          <button
            key={platform}
            type="button"
            aria-pressed={isSelected}
            onClick={() => { toggle(platform); }}
            className={chipClass(isSelected)}
          >
            {isSelected ? <Check className="size-3.5" aria-hidden /> : <Plus className="size-3.5" aria-hidden />}
            {platformLabel(platform)}
            {isSuggested ? (
              <span className="rounded-full bg-brand/15 px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide text-brand">
                Suggested
              </span>
            ) : null}
          </button>
        );
      })}
    </div>
  );
}
