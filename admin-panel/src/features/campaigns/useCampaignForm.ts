import { useState } from 'react';
import type { CampaignInput } from '@/shared/types/domain';

export const OBJECTIVES: readonly { readonly key: string; readonly label: string }[] = [
  { key: 'lead', label: 'Lead generation' },
  { key: 'traffic', label: 'Website traffic' },
  { key: 'awareness', label: 'Brand awareness' },
];

export const PLATFORMS: readonly string[] = [
  'instagram', 'youtube', 'telegram', 'x', 'linkedin', 'reddit',
];

/** The three seed inputs are shared form state, but each platform discovers
 *  differently, so only the relevant ones are shown (with platform-appropriate
 *  labels) and `requireAnyOf` enforces the minimum the engine needs to run. */
export type SeedKey = 'seedHashtags' | 'seedAccounts' | 'seedChannels';

export interface SeedFieldDef {
  readonly key: SeedKey;
  readonly label: string;
  readonly placeholder: string;
  readonly hint?: string;
}

export interface PlatformSeedConfig {
  readonly fields: readonly SeedFieldDef[];
  // At least one of these must be non-empty to save. Omitted ⇒ seeds optional
  // (Instagram also has the home feed, so it can run with none).
  readonly requireAnyOf?: readonly SeedKey[];
}

export const PLATFORM_SEEDS: Readonly<Record<string, PlatformSeedConfig>> = {
  instagram: {
    fields: [
      { key: 'seedHashtags', label: 'Seed hashtags', placeholder: 'projectmanagement, saas' },
      { key: 'seedAccounts', label: 'Seed accounts', placeholder: 'acme.io' },
    ],
  },
  youtube: {
    fields: [
      { key: 'seedHashtags', label: 'Search queries', placeholder: 'best project management app' },
      { key: 'seedChannels', label: 'Seed channel IDs', placeholder: 'UCabc123, UCdef456' },
    ],
    requireAnyOf: ['seedHashtags', 'seedChannels'],
  },
  telegram: {
    fields: [
      {
        key: 'seedChannels',
        label: 'Channels to monitor',
        placeholder: '@product_updates, @saas_chat',
        hint: 'Public channels read by @username — no admin step needed.',
      },
    ],
    requireAnyOf: ['seedChannels'],
  },
  // X and LinkedIn are managed-CDP with an algorithmic home feed, so seeds are
  // optional (no requireAnyOf, like Instagram). Labels match the engine mapping:
  // seedAccounts → handles/people-or-company slugs, seedHashtags → saved
  // searches / hashtags (engines/{x,linkedin}/cdp.py walk these as sources).
  x: {
    fields: [
      { key: 'seedHashtags', label: 'Saved searches / hashtags', placeholder: 'project management tools' },
      { key: 'seedAccounts', label: 'Accounts / List members', placeholder: '@acme, @saastools' },
    ],
  },
  linkedin: {
    fields: [
      { key: 'seedHashtags', label: 'Seed hashtags', placeholder: 'saas, productivity' },
      { key: 'seedAccounts', label: 'People / companies', placeholder: 'in/jane-doe, company/acme' },
    ],
  },
  // Reddit is API-seeded: subreddits are required (no algorithmic feed), and
  // seedHashtags are reused as per-subreddit search queries (dispatch.py).
  reddit: {
    fields: [
      {
        key: 'seedChannels',
        label: 'Subreddits',
        placeholder: 'r/SaaS, r/projectmanagement',
        hint: 'Subreddits to walk — the seed list IS the discovery surface.',
      },
      { key: 'seedHashtags', label: 'Search terms (optional)', placeholder: 'project management, task tracker' },
    ],
    requireAnyOf: ['seedChannels'],
  },
};

const FALLBACK_SEED_CONFIG: PlatformSeedConfig = { fields: [] };

/** The seed config for a platform (never throws on an unknown platform). */
export function seedConfigFor(platform: string): PlatformSeedConfig {
  return PLATFORM_SEEDS[platform] ?? FALLBACK_SEED_CONFIG;
}

const DEFAULT_BUDGET_CAP = 7500;
const DEFAULT_GOAL_TARGET = 200;
const DEFAULT_THRESHOLD = 0.7;

/**
 * One platform a campaign fans out to, in FORM shape: seeds are comma STRINGS for
 * text input (the wire `ChannelEntry` uses arrays). No `includeHomeFeed` — the
 * engine derives it seed-aware (multi-platform plan M2/M7). Named ChannelFormEntry,
 * NOT ChannelEntry, to stay distinct from the wire type.
 */
export interface ChannelFormEntry {
  readonly platform: string;
  readonly seedHashtags: string;
  readonly seedAccounts: string;
  readonly seedChannels: string;
}

/** A blank channel row for a platform (used when the user adds one). */
export function blankChannel(platform: string): ChannelFormEntry {
  return { platform, seedHashtags: '', seedAccounts: '', seedChannels: '' };
}

export interface CampaignFormState {
  readonly name: string;
  readonly objective: string;
  readonly budgetCap: number;
  readonly goalTarget: number;
  // Brief fields. Array fields are kept as comma strings for easy text input.
  readonly platform: string;
  readonly threshold: number;
  readonly languages: string;
  readonly relevanceDef: string;
  readonly matchDef: string;
  readonly extractDef: string;
  // Advanced, optional tuned classifier prompts. Blank ⇒ keep the existing
  // prompt (edit) or use the engine's generic fallback (create).
  readonly relevancePrompt: string;
  readonly matchPrompt: string;
  readonly visionPrompt: string;
  readonly seedHashtags: string;
  readonly seedAccounts: string;
  readonly seedChannels: string;
  // Multi-platform fan-out. Empty ⇒ legacy single-platform (the flat fields above
  // drive the brief). Length ≥2 ⇒ the campaign runs each channel; toInput then
  // emits a `channels` array and sources the flat scalars from channels[0] (L2).
  readonly channels: readonly ChannelFormEntry[];
}

const INITIAL_STATE: CampaignFormState = {
  name: '',
  objective: OBJECTIVES[0]?.key ?? 'lead',
  budgetCap: DEFAULT_BUDGET_CAP,
  goalTarget: DEFAULT_GOAL_TARGET,
  platform: 'instagram',
  threshold: DEFAULT_THRESHOLD,
  languages: '',
  relevanceDef: '',
  matchDef: '',
  extractDef: '',
  relevancePrompt: '',
  matchPrompt: '',
  visionPrompt: '',
  seedHashtags: '',
  seedAccounts: '',
  seedChannels: '',
  channels: [],
};

/** Seed values for edit mode — the engine id is fixed and status is preserved. */
export interface CampaignFormSeed extends CampaignFormState {
  readonly campaignId: string;
  readonly status: string;
}

/**
 * A partial create-form prefill (e.g. AI-drafted). Unlike a `seed` (edit), a
 * draft keeps CREATE semantics: it only sets initial field values, leaving the
 * id derived from the name and the status `draft`.
 */
export type CampaignDraft = Partial<CampaignFormState>;

/** Slug used as the engine campaignId: lowercase, spaces→'-', strip non [a-z0-9-]. */
export function slugify(name: string): string {
  return name
    .toLowerCase()
    .trim()
    .replace(/\s+/g, '-')
    .replace(/[^a-z0-9-]/g, '')
    .replace(/-+/g, '-')
    .replace(/^-|-$/g, '');
}

/** comma/newline-separated text → trimmed, de-blanked string list. */
function splitList(raw: string): string[] {
  return raw
    .split(/[,\n]/)
    .map((s) => s.trim())
    .filter((s) => s.length > 0);
}

/** Whether one channel's seeds satisfy its platform's minimum requirement. */
function channelHasRequiredSeeds(
  platform: string,
  seeds: Pick<CampaignFormState, SeedKey>,
): boolean {
  const required = seedConfigFor(platform).requireAnyOf;
  if (!required) return true;
  return required.some((key) => splitList(seeds[key]).length > 0);
}

/** Whether the form satisfies its seed requirement. Single-platform (no channels):
 *  the flat fields. Multi-platform: EVERY channel must independently satisfy its
 *  own platform's requirement (one underseeded channel fails the whole form). */
function hasRequiredSeeds(form: CampaignFormState): boolean {
  if (form.channels.length === 0) {
    return channelHasRequiredSeeds(form.platform, form);
  }
  return form.channels.every((ch) => channelHasRequiredSeeds(ch.platform, ch));
}

export interface UseCampaignForm {
  readonly form: CampaignFormState;
  readonly campaignId: string;
  readonly isEdit: boolean;
  readonly isValid: boolean;
  readonly update: (patch: Partial<CampaignFormState>) => void;
  readonly toInput: () => CampaignInput;
}

/** Local state for the campaign form. With no `seed` it's a create form (the
 *  engine id is slugified from the name, status starts as `draft`); with a
 *  `seed` it's an edit form (id fixed to the engine key, status preserved). An
 *  optional `draft` prefills a create form (e.g. AI-drafted) without switching
 *  to edit semantics. `useState` reads its initializer once, so a draft must be
 *  present on first render — mount this hook only after the draft exists. */
export function useCampaignForm(seed?: CampaignFormSeed, draft?: CampaignDraft): UseCampaignForm {
  const isEdit = seed !== undefined;
  const [form, setForm] = useState<CampaignFormState>(seed ?? { ...INITIAL_STATE, ...draft });

  // Edit keeps the engine id stable (renaming changes only the display name);
  // create derives it from the name.
  const campaignId = isEdit ? seed.campaignId : slugify(form.name);
  const isValid =
    form.name.trim().length > 0 && campaignId.length > 0 && hasRequiredSeeds(form);

  return {
    form,
    campaignId,
    isEdit,
    isValid,
    update: (patch) => { setForm((prev) => ({ ...prev, ...patch })); },
    toInput: () => ({
      campaignId,
      // Name the intent instead of letting the bridge infer it from existence: a
      // create whose slug collides with an existing campaign must be refused (409,
      // "rename it or edit the existing campaign") rather than overwrite that
      // campaign's brief and inherit its leads.
      op: isEdit ? ('edit' as const) : ('create' as const),
      displayName: form.name.trim(),
      status: isEdit ? seed.status : 'draft',
      budgetCap: form.budgetCap,
      goalTarget: form.goalTarget,
      brief: briefFromForm(form),
    }),
  };
}

/**
 * Form state → the wire brief. L2 collapse rule: a `channels` array is emitted ONLY
 * for a real multi-platform campaign (≥2 channels); 0 or 1 channels collapse to the
 * flat single-platform brief with NO `channels` key. When ≥2, the flat scalars are
 * sourced from the FIRST channel (mirroring the engine's campaign_to_brief), so the
 * primary platform stays consistent with channels[0].
 */
function briefFromForm(form: CampaignFormState) {
  const base = {
    platform: form.platform,
    goal: form.objective,
    threshold: form.threshold,
    languageMix: splitList(form.languages),
    relevanceDef: form.relevanceDef.trim(),
    matchDef: form.matchDef.trim(),
    extractDef: form.extractDef.trim(),
    relevancePrompt: form.relevancePrompt.trim(),
    matchPrompt: form.matchPrompt.trim(),
    visionPrompt: form.visionPrompt.trim(),
    seedHashtags: splitList(form.seedHashtags),
    seedAccounts: splitList(form.seedAccounts),
    seedChannels: splitList(form.seedChannels),
  };
  const first = form.channels[0];
  if (form.channels.length < 2 || !first) return base;
  return {
    ...base,
    platform: first.platform,
    seedHashtags: splitList(first.seedHashtags),
    seedAccounts: splitList(first.seedAccounts),
    seedChannels: splitList(first.seedChannels),
    channels: form.channels.map((ch) => ({
      platform: ch.platform,
      seedHashtags: splitList(ch.seedHashtags),
      seedAccounts: splitList(ch.seedAccounts),
      seedChannels: splitList(ch.seedChannels),
    })),
  };
}
