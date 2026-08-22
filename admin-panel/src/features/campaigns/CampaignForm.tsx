import { useLayoutEffect, useRef, useState } from 'react';
import { Info, Plus, X } from 'lucide-react';
import { Link } from 'react-router-dom';
import { Button } from '@/shared/ui/Button';
import { Card, CardBody } from '@/shared/ui/Card';
import { Modal } from '@/shared/ui/Modal';
import { cn } from '@/shared/lib/cn';
import { platformLabel } from '@/shared/lib/platformLabel';
import {
  blankChannel,
  OBJECTIVES,
  PLATFORMS,
  seedConfigFor,
  type ChannelFormEntry,
  type SeedKey,
  type UseCampaignForm,
} from './useCampaignForm';
import { FIELD_CLASS, LABEL_CLASS } from './formStyles';

// Platforms driven through the shared warmed-Chrome browser (engine C4). Running
// more than one of these in a single campaign means Chrome must be logged into
// each — surfaced as a warning, not a block (the operator may have done so).
const CDP_PLATFORMS = new Set(['instagram', 'x', 'linkedin']);

interface CampaignFormProps {
  readonly api: UseCampaignForm;
  readonly onSubmit: () => void;
  readonly isPending: boolean;
  readonly isError: boolean;
  readonly errorMessage?: string | undefined;
  readonly submitLabel: string;
  readonly note?: string | undefined;
}

type SeedValues = Pick<ChannelFormEntry, SeedKey>;

// Seed lists are long (dozens of hashtags/handles), so they get a multi-line box
// that grows with its content instead of a one-line input that hides everything
// past the first few entries. Capped so a huge paste can't push the rest of the
// form off-screen — past the cap the textarea scrolls.
const SEED_MAX_HEIGHT_PX = 220;

/** A seed-list textarea that auto-sizes to its content (min 3 rows, capped). */
function SeedTextarea({
  id, value, onChange, placeholder,
}: {
  readonly id: string;
  readonly value: string;
  readonly onChange: (value: string) => void;
  readonly placeholder: string;
}) {
  const ref = useRef<HTMLTextAreaElement>(null);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = 'auto';
    const next = Math.min(el.scrollHeight, SEED_MAX_HEIGHT_PX);
    // jsdom reports scrollHeight 0; leaving the height unset keeps the CSS min.
    el.style.height = next > 0 ? `${next}px` : '';
  }, [value]);

  return (
    <textarea
      ref={ref}
      id={id}
      rows={3}
      value={value}
      onChange={(e) => { onChange(e.target.value); }}
      placeholder={placeholder}
      spellCheck={false}
      className={cn(FIELD_CLASS, 'min-h-[5.25rem] resize-y leading-relaxed')}
    />
  );
}

/** Platform-specific seed inputs for ONE platform (the flat single-platform form,
 *  or one channel of a fan-out). Each platform discovers differently, so only the
 *  relevant fields show (Telegram needs channels; YouTube needs queries or channel
 *  ids; Instagram uses hashtags + accounts). `idPrefix` namespaces the input ids so
 *  two channels of the SAME platform don't collide on `htmlFor` (L3). */
function SeedFields({
  platform, values, onChange, idPrefix,
}: {
  readonly platform: string;
  readonly values: SeedValues;
  readonly onChange: (key: SeedKey, value: string) => void;
  readonly idPrefix: string;
}) {
  const config = seedConfigFor(platform);
  if (config.fields.length === 0) return null;

  return (
    <div>
      <div className="grid grid-cols-1 gap-4">
        {config.fields.map((field) => (
          <div key={field.key}>
            <label htmlFor={`seed-${idPrefix}-${field.key}`} className={LABEL_CLASS}>{field.label}</label>
            <SeedTextarea
              id={`seed-${idPrefix}-${field.key}`}
              value={values[field.key]}
              onChange={(value) => { onChange(field.key, value); }}
              placeholder={field.placeholder}
            />
            {field.hint ? <p className="mt-1.5 text-[11px] text-text-faint">{field.hint}</p> : null}
          </div>
        ))}
      </div>
      <p className="mt-2 text-[11px] text-text-faint">
        Separate entries with a comma or a new line.
      </p>
      {config.requireAnyOf ? (
        <p className="mt-2 text-[11px] font-semibold text-text-faint">
          {config.requireAnyOf.length > 1 ? 'At least one is required to run.' : 'Required to run.'}
        </p>
      ) : null}
    </div>
  );
}

/** True when a channel carries any seed input (so removing it should confirm). */
function channelHasSeeds(ch: ChannelFormEntry): boolean {
  return Boolean(ch.seedHashtags.trim() || ch.seedAccounts.trim() || ch.seedChannels.trim());
}

/** First platform not already used by a channel, else instagram. */
function nextUnusedPlatform(used: readonly string[]): string {
  return PLATFORMS.find((p) => !used.includes(p)) ?? 'instagram';
}

/**
 * Unified platforms editor — the single source of platform truth on the form. The
 * platform(s) the campaign searches always render as cards (seeded by the AI draft
 * or by hand), each with its own seeds. A single-platform campaign (the flat brief
 * fields, `channels` empty) shows as ONE non-removable card backed by those flat
 * fields; "Add platform" converts it into a real ≥2 fan-out. Removing back down to a
 * single platform collapses to the flat representation. Removing a seeded platform
 * confirms first (Modal). >1 CDP platform surfaces the shared-Chrome warning.
 */
function PlatformChannels({ api }: { readonly api: UseCampaignForm }) {
  const { form, update } = api;
  const channels = form.channels;
  const isFlat = channels.length === 0;
  const [pendingRemove, setPendingRemove] = useState<number | null>(null);

  // The rows to render: in flat mode, one synthetic row mirroring the flat fields.
  const flatRow: ChannelFormEntry = {
    platform: form.platform,
    seedHashtags: form.seedHashtags,
    seedAccounts: form.seedAccounts,
    seedChannels: form.seedChannels,
  };
  const rows: readonly ChannelFormEntry[] = isFlat ? [flatRow] : channels;

  // Editing a row writes to the flat fields (single) or to channels[i] (fan-out).
  const setRow = (i: number, patch: Partial<ChannelFormEntry>) => {
    if (isFlat) update(patch);
    else update({ channels: channels.map((c, j) => (j === i ? { ...c, ...patch } : c)) });
  };
  const addChannel = () => {
    if (isFlat) {
      // Promote the flat single platform to channel[0], add a blank channel[1] so
      // the campaign genuinely fans out (≥2).
      update({ channels: [flatRow, blankChannel(nextUnusedPlatform([form.platform]))] });
      return;
    }
    update({ channels: [...channels, blankChannel(nextUnusedPlatform(channels.map((c) => c.platform)))] });
  };
  const removeChannel = (i: number) => {
    const next = channels.filter((_, j) => j !== i);
    const only = next[0];
    if (next.length === 1 && only) {
      // Collapse back to the flat single-platform representation (L2 invariant:
      // channels is 0 or ≥2, never 1).
      update({
        platform: only.platform,
        seedHashtags: only.seedHashtags,
        seedAccounts: only.seedAccounts,
        seedChannels: only.seedChannels,
        channels: [],
      });
    } else {
      update({ channels: next });
    }
  };
  const requestRemove = (i: number) => {
    const ch = channels[i];
    if (ch && channelHasSeeds(ch)) setPendingRemove(i);
    else removeChannel(i);
  };

  const cdpCount = rows.filter((c) => CDP_PLATFORMS.has(c.platform)).length;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <div>
          <span className={LABEL_CLASS}>Platforms</span>
          <p className="text-[11px] text-text-faint">
            {isFlat
              ? 'Where this campaign searches for leads. Add more to run across several platforms in one campaign.'
              : 'Each platform runs its own discovery pass, one after another.'}
          </p>
        </div>
        <button
          type="button"
          onClick={addChannel}
          className="inline-flex items-center gap-1.5 rounded-full border border-border bg-surface px-3 py-1.5 text-xs font-bold text-text shadow-tile transition hover:-translate-y-px hover:shadow-lift active:scale-95"
        >
          <Plus className="size-3.5" aria-hidden />
          Add platform
        </button>
      </div>

      {cdpCount > 1 ? (
        <p
          role="alert"
          className="flex items-start gap-2 rounded-tile border border-warn/40 bg-warn-soft px-3.5 py-3 text-[11px] font-semibold text-warn"
        >
          <Info className="mt-0.5 size-4 shrink-0" aria-hidden />
          More than one browser platform (Instagram, X, LinkedIn) is configured. The warmed
          Chrome must be logged into each — they run sequentially in one session.
        </p>
      ) : null}

      {rows.map((ch, i) => {
        // Flat mode keeps the id `platform` so the single select is still labelled
        // "Platform" (one coherent control); fan-out rows namespace by index.
        const selectId = isFlat ? 'platform' : `channel-platform-${i}`;
        return (
          <div key={isFlat ? 'flat' : i} className="rounded-tile border border-border bg-surface-2/40 p-4">
            <div className="mb-3 flex items-center justify-between gap-3">
              <div className="min-w-0 flex-1">
                <label htmlFor={selectId} className={LABEL_CLASS}>Platform</label>
                <select
                  id={selectId}
                  value={ch.platform}
                  onChange={(e) => { setRow(i, { platform: e.target.value }); }}
                  className={FIELD_CLASS}
                >
                  {PLATFORMS.map((p) => (
                    <option key={p} value={p}>{platformLabel(p)}</option>
                  ))}
                </select>
              </div>
              {isFlat ? null : (
                <button
                  type="button"
                  onClick={() => { requestRemove(i); }}
                  aria-label={`Remove ${platformLabel(ch.platform)} channel`}
                  className="mt-5 inline-flex size-8 shrink-0 items-center justify-center rounded-full border border-border bg-surface text-text-muted transition hover:text-danger active:scale-95"
                >
                  <X className="size-4" aria-hidden />
                </button>
              )}
            </div>
            <SeedFields
              platform={ch.platform}
              values={ch}
              onChange={(key, value) => { setRow(i, { [key]: value }); }}
              idPrefix={isFlat ? 'flat' : String(i)}
            />
          </div>
        );
      })}

      <Modal
        isOpen={pendingRemove !== null}
        onClose={() => { setPendingRemove(null); }}
        title="Remove this platform?"
        footer={
          <>
            <button
              type="button"
              onClick={() => { setPendingRemove(null); }}
              className="inline-flex items-center rounded-full border border-border bg-surface px-3.5 py-1.5 text-xs font-bold text-text shadow-tile transition hover:-translate-y-px active:scale-95"
            >
              Keep it
            </button>
            <Button
              type="button"
              onClick={() => {
                if (pendingRemove !== null) removeChannel(pendingRemove);
                setPendingRemove(null);
              }}
            >
              Discard channel
            </Button>
          </>
        }
      >
        <p className="text-[13px] text-text-muted">
          This platform has seed inputs. Removing it discards those seeds for this campaign.
        </p>
      </Modal>
    </div>
  );
}

// The three tuned classifier prompts: optional power-user overrides. Left blank,
// the engine uses its generic prompt (and an edit save never wipes an existing
// tuned prompt — blank means "leave as-is" server-side).
const PROMPT_FIELDS: readonly {
  readonly key: 'relevancePrompt' | 'matchPrompt' | 'visionPrompt';
  readonly label: string;
  readonly hint: string;
}[] = [
  { key: 'relevancePrompt', label: 'Relevance prompt', hint: 'System prompt for the reel relevance gate.' },
  { key: 'matchPrompt', label: 'Match prompt', hint: 'System prompt for scoring a comment as a lead and extracting fields.' },
  { key: 'visionPrompt', label: 'Vision prompt', hint: 'System prompt for reading on-screen text in frames.' },
];

/** Optional tuned system prompts, tucked behind a disclosure so the common path
 *  stays short. Blank = engine default; an edit save won't clobber stored prompts. */
function AdvancedPrompts({ api }: { readonly api: UseCampaignForm }) {
  const { form, update } = api;
  return (
    <details className="rounded-tile border border-border bg-surface-2/40">
      <summary className="cursor-pointer select-none px-3.5 py-3 text-[13px] font-bold text-text-muted">
        Advanced — classifier prompts (optional)
      </summary>
      <div className="flex flex-col gap-4 border-t border-border p-4">
        <p className="text-[11px] text-text-faint">
          Override the AI’s system prompts for finer control. Leave blank to use the engine’s
          built-in defaults — saving with these blank won’t erase a campaign’s existing prompts.
        </p>
        {PROMPT_FIELDS.map(({ key, label, hint }) => (
          <div key={key}>
            <label htmlFor={`prompt-${key}`} className={LABEL_CLASS}>{label}</label>
            <textarea
              id={`prompt-${key}`}
              rows={4}
              value={form[key]}
              onChange={(e) => { update({ [key]: e.target.value }); }}
              placeholder="Leave blank to use the engine default"
              className={cn(FIELD_CLASS, 'font-mono text-xs')}
            />
            <p className="mt-1.5 text-[11px] text-text-faint">{hint}</p>
          </div>
        ))}
      </div>
    </details>
  );
}

/** Shared campaign form body (create + edit). The page supplies the header,
 *  the mutation wiring, and the submit/error copy; this owns the fields. */
export function CampaignForm({
  api, onSubmit, isPending, isError, errorMessage, submitLabel, note,
}: CampaignFormProps) {
  const { form, campaignId, isEdit, isValid, update } = api;

  return (
    <form
      onSubmit={(e) => { e.preventDefault(); if (isValid && !isPending) onSubmit(); }}
      className="mx-auto max-w-2xl"
    >
      <Card>
        <CardBody className="flex flex-col gap-6 p-6">
          <div>
            <label htmlFor="campaign-name" className={LABEL_CLASS}>Campaign name</label>
            <input
              id="campaign-name"
              type="text"
              value={form.name}
              onChange={(e) => { update({ name: e.target.value }); }}
              placeholder="e.g. Summer Launch Blitz"
              autoComplete="off"
              className={FIELD_CLASS}
            />
            <p className="mt-1.5 text-[11px] text-text-faint">
              {isEdit ? (
                <>Engine id <span className="font-mono text-text-muted">{campaignId}</span> is fixed.</>
              ) : (
                <>Engine id: <span className="font-mono text-text-muted">{campaignId || '—'}</span></>
              )}
            </p>
          </div>

          <div>
            <span className={LABEL_CLASS}>Objective</span>
            <div className="grid grid-cols-3 gap-2 max-md:grid-cols-1">
              {OBJECTIVES.map(({ key, label }) => {
                const isSelected = form.objective === key;
                return (
                  <button
                    key={key}
                    type="button"
                    onClick={() => { update({ objective: key }); }}
                    aria-pressed={isSelected}
                    className={cn(
                      'rounded-tile border-2 px-3 py-3 text-left text-[13.5px] font-bold transition',
                      isSelected
                        ? 'border-brand bg-brand/10 text-text'
                        : 'border-border bg-surface text-text-muted hover:border-border hover:text-text',
                    )}
                  >
                    {label}
                  </button>
                );
              })}
            </div>
          </div>

          {/* Campaign-wide numeric settings. Platform selection lives in the unified
              Platforms editor below (one coherent control, no separate dropdown). */}
          <div className="grid grid-cols-3 gap-4 max-md:grid-cols-1">
            <div>
              <label htmlFor="threshold" className={LABEL_CLASS}>Match threshold (0–1)</label>
              <input
                id="threshold"
                type="number"
                min={0}
                max={1}
                step="any"
                value={form.threshold}
                onChange={(e) => { update({ threshold: Number(e.target.value) }); }}
                className={cn(FIELD_CLASS, 'tabular-nums')}
              />
            </div>
            <div>
              <label htmlFor="budget-cap" className={LABEL_CLASS}>Budget cap (USD)</label>
              <input
                id="budget-cap"
                type="number"
                min={0}
                step="any"
                value={form.budgetCap}
                onChange={(e) => { update({ budgetCap: Number(e.target.value) }); }}
                className={cn(FIELD_CLASS, 'tabular-nums')}
              />
            </div>
            <div>
              <label htmlFor="goal-target" className={LABEL_CLASS}>Monthly goal target (leads)</label>
              <input
                id="goal-target"
                type="number"
                min={0}
                step="any"
                value={form.goalTarget}
                onChange={(e) => { update({ goalTarget: Number(e.target.value) }); }}
                className={cn(FIELD_CLASS, 'tabular-nums')}
              />
            </div>
          </div>

          <div>
            <label htmlFor="languages" className={LABEL_CLASS}>Languages (comma-separated)</label>
            <input
              id="languages"
              type="text"
              value={form.languages}
              onChange={(e) => { update({ languages: e.target.value }); }}
              placeholder="uz, ru, en"
              className={FIELD_CLASS}
            />
          </div>

          <div>
            <label htmlFor="relevance-def" className={LABEL_CLASS}>Relevance — what counts as on-topic?</label>
            <textarea
              id="relevance-def"
              rows={3}
              value={form.relevanceDef}
              onChange={(e) => { update({ relevanceDef: e.target.value }); }}
              placeholder="Posts about a SaaS product — demos, pricing, features, free trials…"
              className={FIELD_CLASS}
            />
          </div>

          <div>
            <label htmlFor="match-def" className={LABEL_CLASS}>Match — what makes a comment a lead?</label>
            <textarea
              id="match-def"
              rows={3}
              value={form.matchDef}
              onChange={(e) => { update({ matchDef: e.target.value }); }}
              placeholder="The commenter signals intent to buy / subscribe / try it, or asks price / features…"
              className={FIELD_CLASS}
            />
          </div>

          <div>
            <label htmlFor="extract-def" className={LABEL_CLASS}>Extract — fields to pull from a lead</label>
            <textarea
              id="extract-def"
              rows={3}
              value={form.extractDef}
              onChange={(e) => { update({ extractDef: e.target.value }); }}
              placeholder={'- phone\n- email\n- intent'}
              className={cn(FIELD_CLASS, 'font-mono text-xs')}
            />
            <p className="mt-1.5 text-[11px] text-text-faint">
              One field per line, e.g. <span className="font-mono">- phone</span> or{' '}
              <span className="font-mono">{'- `email` — work email'}</span>. Each line becomes a key
              the AI must return for every lead.
            </p>
          </div>

          <PlatformChannels api={api} />

          <AdvancedPrompts api={api} />

          {note ? (
            <p className="flex items-start gap-2 rounded-tile bg-surface-2 px-3.5 py-3 text-xs text-text-muted">
              <Info className="mt-0.5 size-4 shrink-0 text-text-faint" aria-hidden />
              {note}
            </p>
          ) : null}

          {isError ? (
            <p className="text-xs font-semibold text-danger">
              {errorMessage ?? 'Could not save the campaign. Please try again.'}
            </p>
          ) : null}

          <div className="flex items-center justify-end gap-2 border-t border-border pt-4">
            <Link
              to="/campaigns"
              className="inline-flex items-center rounded-full border border-border bg-surface px-3.5 py-1.5 text-xs font-bold text-text shadow-tile transition hover:-translate-y-px hover:shadow-lift active:scale-95"
            >
              Cancel
            </Link>
            <Button type="submit" disabled={!isValid || isPending}>
              {isPending ? 'Saving…' : submitLabel}
            </Button>
          </div>
        </CardBody>
      </Card>
    </form>
  );
}
