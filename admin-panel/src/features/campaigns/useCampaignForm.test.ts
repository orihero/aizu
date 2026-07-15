import { describe, expect, test } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import {
  blankChannel,
  useCampaignForm,
  type CampaignFormSeed,
} from './useCampaignForm';

/** A complete edit seed so we can drive multi-channel state through `update`. */
function editSeed(overrides: Partial<CampaignFormSeed> = {}): CampaignFormSeed {
  return {
    campaignId: 'multi-campaign',
    status: 'live',
    name: 'Multi Campaign',
    objective: 'lead',
    budgetCap: 7500,
    goalTarget: 200,
    platform: 'instagram',
    threshold: 0.7,
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
    ...overrides,
  };
}

describe('useCampaignForm — multi-platform (Phase 5)', () => {
  test('INITIAL_STATE has an empty channels list (create form)', () => {
    const { result } = renderHook(() => useCampaignForm());
    expect(result.current.form.channels).toEqual([]);
  });

  test('a single-platform form emits NO channels key (flat brief)', () => {
    const { result } = renderHook(() => useCampaignForm(editSeed()));
    const brief = result.current.toInput().brief as Record<string, unknown>;
    expect('channels' in brief).toBe(false);
    expect(brief.platform).toBe('instagram');
  });

  test('one channel still collapses to a flat brief with NO channels key (L2)', () => {
    const { result } = renderHook(() =>
      useCampaignForm(editSeed({ channels: [blankChannel('youtube')] })),
    );
    const brief = result.current.toInput().brief as Record<string, unknown>;
    expect('channels' in brief).toBe(false);
  });

  test('two channels emit a channels array and source flat scalars from channels[0]', () => {
    const seed = editSeed({
      channels: [
        { platform: 'youtube', seedHashtags: 'a', seedAccounts: '', seedChannels: 'UC1' },
        { platform: 'instagram', seedHashtags: 'b', seedAccounts: '', seedChannels: '' },
      ],
    });
    const { result } = renderHook(() => useCampaignForm(seed));
    const brief = result.current.toInput().brief as Record<string, unknown>;
    const channels = brief.channels as { platform: string }[];
    expect(channels.map((c) => c.platform)).toEqual(['youtube', 'instagram']);
    // Flat scalars mirror channels[0] (the engine's campaign_to_brief contract).
    expect(brief.platform).toBe('youtube');
    expect(brief.seedChannels).toEqual(['UC1']);
  });

  test('isValid fails when ANY channel is underseeded (per-channel requireAnyOf)', () => {
    // youtube requires a hashtag/channel; an empty youtube channel must fail the form.
    const { result } = renderHook(() =>
      useCampaignForm(editSeed({
        channels: [
          { platform: 'instagram', seedHashtags: '', seedAccounts: '', seedChannels: '' },
          { platform: 'youtube', seedHashtags: '', seedAccounts: '', seedChannels: '' },
        ],
      })),
    );
    expect(result.current.isValid).toBe(false);
    // Seed the youtube channel → the whole form becomes valid.
    act(() => {
      result.current.update({
        channels: [
          { platform: 'instagram', seedHashtags: '', seedAccounts: '', seedChannels: '' },
          { platform: 'youtube', seedHashtags: '', seedAccounts: '', seedChannels: 'UC1' },
        ],
      });
    });
    expect(result.current.isValid).toBe(true);
  });

  test('single-platform validity still uses the flat seed requirement', () => {
    // telegram requires seedChannels; the flat path (no channels) must enforce it.
    const { result } = renderHook(() =>
      useCampaignForm(editSeed({ platform: 'telegram', seedChannels: '' })),
    );
    expect(result.current.isValid).toBe(false);
  });
});
