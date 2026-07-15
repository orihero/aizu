import { describe, expect, test } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { buildCampaign, buildPanelState } from '@/test/fixtures';
import { FakePanelRepository } from '@/test/fakePanelRepository';
import { renderWithProviders } from '@/test/renderWithProviders';
import { CampaignCard } from './CampaignCard';
import { CampaignForm } from './CampaignForm';
import {
  useCampaignForm,
  type CampaignFormSeed,
  type ChannelFormEntry,
} from './useCampaignForm';

function seed(overrides: Partial<CampaignFormSeed> = {}): CampaignFormSeed {
  return {
    campaignId: 'c1', status: 'live', name: 'C1', objective: 'lead',
    budgetCap: 7500, goalTarget: 200, platform: 'instagram', threshold: 0.7,
    languages: '', relevanceDef: '', matchDef: '', extractDef: '',
    relevancePrompt: '', matchPrompt: '', visionPrompt: '',
    seedHashtags: '', seedAccounts: '', seedChannels: '', channels: [],
    ...overrides,
  };
}

function ch(platform: string, seeds: Partial<ChannelFormEntry> = {}): ChannelFormEntry {
  return { platform, seedHashtags: '', seedAccounts: '', seedChannels: '', ...seeds };
}

function FormHarness({ initial }: { readonly initial: CampaignFormSeed }) {
  const api = useCampaignForm(initial);
  return (
    <CampaignForm api={api} onSubmit={() => {}} isPending={false} isError={false}
      submitLabel="Save" />
  );
}

function renderForm(initial: CampaignFormSeed) {
  renderWithProviders(<FormHarness initial={initial} />, {
    repository: new FakePanelRepository(buildPanelState()),
  });
}

describe('CampaignForm — multi-platform UI (Phase 6)', () => {
  test('single-platform mode renders the legacy platform select', () => {
    renderForm(seed());
    expect(screen.getByLabelText('Platform')).toBeInTheDocument();
    expect(screen.queryByLabelText(/^channel-platform/)).not.toBeInTheDocument();
  });

  test('Add platform converts to per-channel sections (≥2 channels)', async () => {
    const user = userEvent.setup();
    renderForm(seed());
    await user.click(screen.getByRole('button', { name: /add platform/i }));
    // Two channel platform selects now exist; the flat one is gone.
    expect(screen.getByLabelText('Match threshold (0–1)')).toBeInTheDocument();
    expect(screen.getAllByLabelText('Platform')).toHaveLength(2); // both channel selects
  });

  test('removing an EMPTY channel does not prompt a confirm dialog', async () => {
    const user = userEvent.setup();
    renderForm(seed({ channels: [ch('instagram'), ch('youtube')] }));
    const removes = screen.getAllByRole('button', { name: /remove .* channel/i });
    await user.click(removes[0]!);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.getAllByLabelText('Platform')).toHaveLength(1); // one channel left
  });

  test('removing a SEEDED channel opens a confirm dialog', async () => {
    const user = userEvent.setup();
    renderForm(seed({ channels: [ch('instagram', { seedHashtags: 'projectmanagement' }), ch('youtube')] }));
    const removes = screen.getAllByRole('button', { name: /remove .* channel/i });
    await user.click(removes[0]!);
    expect(await screen.findByRole('dialog')).toBeInTheDocument();
    expect(screen.getByText(/remove this platform/i)).toBeInTheDocument();
  });

  test('a >1-CDP combo shows a role="alert" warning that clears at one CDP', async () => {
    const user = userEvent.setup();
    renderForm(seed({ channels: [ch('instagram'), ch('x')] }));
    expect(screen.getByRole('alert')).toBeInTheDocument();
    // Switch the X channel to YouTube (an API platform) → only one CDP platform left.
    const selects = screen.getAllByLabelText('Platform');
    await user.selectOptions(selects[1]!, 'youtube');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  test('instagram + linkedin also warns (both CDP)', () => {
    renderForm(seed({ channels: [ch('instagram'), ch('linkedin')] }));
    expect(screen.getByRole('alert')).toBeInTheDocument();
  });

  test('each channel shows its own platform-appropriate seed fields', () => {
    renderForm(seed({ channels: [ch('telegram'), ch('youtube')] }));
    // Telegram exposes "Channels to monitor"; YouTube exposes "Seed channel IDs".
    expect(screen.getByText('Channels to monitor')).toBeInTheDocument();
    expect(screen.getByText('Seed channel IDs')).toBeInTheDocument();
  });
});

describe('CampaignCard — platform chips (Phase 6)', () => {
  const idleRun = { active: null, recent: [] };

  test('renders one chip per platforms entry', () => {
    renderWithProviders(
      <CampaignCard campaign={buildCampaign({ platforms: ['instagram', 'youtube'] })} run={idleRun} />,
      { repository: new FakePanelRepository(buildPanelState()) },
    );
    expect(screen.getByText('Instagram')).toBeInTheDocument();
    expect(screen.getByText('YouTube')).toBeInTheDocument();
  });

  test('falls back to the single platform when platforms is undefined', () => {
    renderWithProviders(
      <CampaignCard campaign={buildCampaign({ platform: 'telegram', platforms: undefined })} run={idleRun} />,
      { repository: new FakePanelRepository(buildPanelState()) },
    );
    expect(screen.getByText('Telegram')).toBeInTheDocument();
  });
});
