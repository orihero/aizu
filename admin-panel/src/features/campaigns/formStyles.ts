/**
 * Shared input/label class strings for the campaign surfaces. Extracted so the
 * form (CampaignForm) and the AI composer render byte-identical fields — keeping
 * one copy prevents the two from drifting apart.
 */

export const FIELD_CLASS =
  'w-full rounded-tile border border-border bg-surface px-3.5 py-2.5 text-sm text-text outline-none transition focus:border-brand focus:ring-2 focus:ring-brand/20 placeholder:text-text-faint';

export const LABEL_CLASS =
  'mb-2 block text-[11.5px] font-bold uppercase tracking-wider text-text-faint';
