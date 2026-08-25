import { describe, expect, test } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {
  buildActiveRun,
  buildCampaign,
  buildIntegration,
  buildMatch,
  buildPanelState,
  buildBilling,
  buildRunActivity,
  buildRunBlock,
  buildRunRecord,
} from '@/test/fixtures';
import { FakePanelRepository } from '@/test/fakePanelRepository';
import { renderWithProviders } from '@/test/renderWithProviders';
import { leadRoute } from '@/shared/lib/leadId';
import type { CampaignBriefForm } from '@/shared/types/domain';
import { LeadsPage } from './leads/LeadsPage';
import { SettingsPage } from './settings/SettingsPage';
import { CampaignNewPage } from './campaigns/CampaignNewPage';
import { CampaignEditPage } from './campaigns/CampaignEditPage';
import { CampaignsPage } from './campaigns/CampaignsPage';

const RUNNABLE_BRIEF: CampaignBriefForm = {
  platform: 'instagram',
  goal: 'lead',
  threshold: 0.7,
  languageMix: ['en'],
  relevanceDef: 'saas product',
  matchDef: 'buyer intent',
  extractDef: '- phone',
  relevancePrompt: '',
  matchPrompt: '',
  visionPrompt: '',
  seedHashtags: [],
  seedAccounts: [],
  seedChannels: [],
};

function runnableCampaign(overrides = {}) {
  return buildCampaign({ status: 'live', briefForm: RUNNABLE_BRIEF, ...overrides });
}

function first<T>(items: readonly T[]): T {
  const value = items[0];
  if (value === undefined) throw new Error('expected at least one element');
  return value;
}

describe('Leads bulk actions', () => {
  test('selecting rows and marking interested records a bulk write', async () => {
    const user = userEvent.setup();
    const repo = new FakePanelRepository(
      buildPanelState({
        MATCHES: [
          buildMatch({ id: 'a', commentId: 'a', status: 'new' }),
          buildMatch({ id: 'b', commentId: 'b', status: 'new' }),
        ],
      }),
    );
    renderWithProviders(<LeadsPage />, { repository: repo, route: '/leads', path: '/leads' });

    const checkboxes = await screen.findAllByRole('checkbox');
    await user.click(first(checkboxes)); // header → selects the whole page

    await user.click(await screen.findByRole('button', { name: /Set status/ }));
    await user.click(await screen.findByRole('menuitem', { name: /Interested/ }));

    await waitFor(() => {
      expect(repo.bulkWrites).toHaveLength(1);
    });
    expect(repo.bulkWrites[0]?.status).toBe('interested');
    expect(repo.bulkWrites[0]?.items).toHaveLength(2);
  });
});

describe('Leads filters', () => {
  test('the campaign filter scopes the leads to one campaign', async () => {
    const user = userEvent.setup();
    const repo = new FakePanelRepository(
      buildPanelState({
        MATCHES: [
          buildMatch({ id: 'a', commentId: 'a', campaignId: 'camp-a', intent: 'alphalead' }),
          buildMatch({ id: 'b', commentId: 'b', campaignId: 'camp-b', intent: 'bravolead' }),
        ],
      }),
    );
    renderWithProviders(<LeadsPage />, { repository: repo, route: '/leads', path: '/leads' });

    expect(await screen.findByText('alphalead')).toBeInTheDocument();
    expect(screen.getByText('bravolead')).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText('Filter by campaign'), 'camp-a');

    await waitFor(() => { expect(screen.queryByText('bravolead')).not.toBeInTheDocument(); });
    expect(screen.getByText('alphalead')).toBeInTheDocument();
    // The server query carried the campaign scope.
    expect(repo.leadsFetches.at(-1)?.campaign).toBe('camp-a');
  });

  test('archived leads are hidden by default and revealed by the Archived filter', async () => {
    const user = userEvent.setup();
    const repo = new FakePanelRepository(
      buildPanelState({
        MATCHES: [
          buildMatch({ id: 'live', commentId: 'live', intent: 'activelead', status: 'new' }),
          buildMatch({ id: 'gone', commentId: 'gone', intent: 'archivedlead', status: 'archived' }),
        ],
      }),
    );
    renderWithProviders(<LeadsPage />, { repository: repo, route: '/leads', path: '/leads' });

    // Default ("All") list omits the archived lead.
    expect(await screen.findByText('activelead')).toBeInTheDocument();
    expect(screen.queryByText('archivedlead')).not.toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText('Filter by status'), 'archived');

    expect(await screen.findByText('archivedlead')).toBeInTheDocument();
    await waitFor(() => { expect(screen.queryByText('activelead')).not.toBeInTheDocument(); });
  });
});

describe('Lead identity is the composite (campaign, platform, comment) key', () => {
  // Two campaigns really can hold the same commenter, and one comment id can repeat
  // across platforms. The panel used to key leads on the bare commentId, so all three
  // rows collapsed into one: clicking a row opened — and wrote status to — whichever
  // copy happened to be first in the list.
  const IG_A = buildMatch({
    commentId: 'dup-1', campaignId: 'cmp-a', platform: 'instagram',
    intent: 'A-side lead', status: 'new',
  });
  const IG_B = buildMatch({
    commentId: 'dup-1', campaignId: 'cmp-b', platform: 'instagram',
    intent: 'B-side lead', status: 'new',
  });
  const X_A = buildMatch({
    commentId: 'dup-1', campaignId: 'cmp-a', platform: 'x',
    intent: 'X-side lead', status: 'new',
  });

  test('all three leads render as distinct rows', async () => {
    const repo = new FakePanelRepository(buildPanelState({ MATCHES: [IG_A, IG_B, X_A] }));
    renderWithProviders(<LeadsPage />, { repository: repo, route: '/leads', path: '/leads' });

    expect(await screen.findByText('A-side lead')).toBeInTheDocument();
    expect(screen.getByText('B-side lead')).toBeInTheDocument();
    expect(screen.getByText('X-side lead')).toBeInTheDocument();
    // ...on three distinct identities (also the React key for each row).
    expect(new Set([IG_A.id, IG_B.id, X_A.id]).size).toBe(3);
  });

  test("clicking a row opens THAT row's lead, not a same-commentId sibling", async () => {
    const user = userEvent.setup();
    const repo = new FakePanelRepository(buildPanelState({ MATCHES: [IG_A, IG_B, X_A] }));
    // Optional param so the row click's own navigation stays inside this route.
    renderWithProviders(<LeadsPage />, {
      repository: repo, route: '/leads', path: '/leads/:leadId?',
    });

    await user.click(await screen.findByText('B-side lead'));

    // The drawer's Source block names the campaign the open lead really belongs to.
    const drawer = within(await screen.findByRole('dialog'));
    expect(drawer.getByText('cmp-b')).toBeInTheDocument();
    expect(drawer.getByText('B-side lead')).toBeInTheDocument();
    expect(drawer.queryByText('A-side lead')).not.toBeInTheDocument();
  });

  test("a status write from the drawer targets the opened campaign's record", async () => {
    const user = userEvent.setup();
    const repo = new FakePanelRepository(buildPanelState({ MATCHES: [IG_A, IG_B, X_A] }));
    renderWithProviders(<LeadsPage />, {
      repository: repo, route: leadRoute(IG_B.id), path: '/leads/:leadId',
    });

    await user.selectOptions(await screen.findByLabelText('Set lead status'), 'interested');

    await waitFor(() => { expect(repo.statusWrites).toHaveLength(1); });
    expect(repo.statusWrites[0]).toMatchObject({
      campaignId: 'cmp-b', platform: 'instagram', commentId: 'dup-1', status: 'interested',
    });
    // ...and only that record moved: the two siblings stay 'new'. The fake applies
    // the write with the same composite key the engine uses, so a write that named
    // only the commentId would visibly flip all three.
    const reread = await repo.fetchLeads({ page: 1, pageSize: 50 });
    const after = new Map(
      (reread.ok ? reread.value.items : []).map((m) => [m.id, m.status]),
    );
    expect(after.get(IG_B.id)).toBe('interested');
    expect(after.get(IG_A.id)).toBe('new');
    expect(after.get(X_A.id)).toBe('new');
  });

  test("a deep link to the second platform's copy opens that copy", async () => {
    const repo = new FakePanelRepository(buildPanelState({ MATCHES: [IG_A, IG_B, X_A] }));
    renderWithProviders(<LeadsPage />, {
      repository: repo, route: leadRoute(X_A.id), path: '/leads/:leadId',
    });

    const drawer = within(await screen.findByRole('dialog'));
    expect(drawer.getByText('X-side lead')).toBeInTheDocument();
    expect(drawer.getByText('cmp-a')).toBeInTheDocument();
    expect(drawer.queryByText('A-side lead')).not.toBeInTheDocument();
  });
});

describe('Lead drawer reel link (revealed leads only)', () => {
  test('links out to the source reel on its platform after an audited reveal', async () => {
    const user = userEvent.setup();
    const lead = buildMatch({ commentId: 'c1', platform: 'instagram' });
    const repo = new FakePanelRepository(buildPanelState({ MATCHES: [lead] }));
    // The reel id is registered on the REVEAL answer, not on the lead: since v27 the
    // anonymized row carries no post pointer, so this is the only place one exists.
    repo.revealIdentities.set(lead.id, { username: 'dana_t', text: 'how much?', reelId: 'DXOML7vjQhn' });
    renderWithProviders(<LeadsPage />, {
      repository: repo,
      route: leadRoute(lead.id),
      path: '/leads/:leadId',
    });

    // v27: no post link until the lead is revealed — the handle and the comment are
    // visible ON that post, so the link is the redaction undone in one click.
    expect(await screen.findByRole('button', { name: /Reveal source/ })).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /DXOML7vjQhn/ })).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /Reveal source/ }));

    const link = await screen.findByRole('link', { name: /DXOML7vjQhn/ });
    expect(link).toHaveAttribute('href', 'https://www.instagram.com/reel/DXOML7vjQhn/');
    expect(link).toHaveAttribute('target', '_blank');
  });

  test('shows the plain reel id when the platform has no derivable URL', async () => {
    const user = userEvent.setup();
    const lead = buildMatch({ commentId: 'c1', platform: 'telegram' });
    const repo = new FakePanelRepository(buildPanelState({ MATCHES: [lead] }));
    repo.revealIdentities.set(lead.id, { username: 'dana_t', text: 'how much?', reelId: 'tg-42' });
    renderWithProviders(<LeadsPage />, {
      repository: repo,
      route: leadRoute(lead.id),
      path: '/leads/:leadId',
    });

    await user.click(await screen.findByRole('button', { name: /Reveal source/ }));

    expect(await screen.findByText('tg-42')).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /tg-42/ })).not.toBeInTheDocument();
  });
});

describe('Settings workspace save', () => {
  test('editing the product name and saving records a settings update', async () => {
    const user = userEvent.setup();
    const repo = new FakePanelRepository(buildPanelState());
    renderWithProviders(<SettingsPage />, { repository: repo, route: '/settings', path: '/settings' });

    const input = await screen.findByDisplayValue('AIZU');
    await user.clear(input);
    await user.type(input, 'LeadFlow');
    await user.click(screen.getByRole('button', { name: /save changes/i }));

    await waitFor(() => {
      expect(repo.settingsUpdates).toHaveLength(1);
    });
    expect(repo.settingsUpdates[0]?.settings).toMatchObject({ productName: 'LeadFlow' });
  });
});

describe('Settings integrations', () => {
  function renderIntegrations(repo: FakePanelRepository) {
    renderWithProviders(<SettingsPage />, {
      repository: repo,
      route: '/settings/integrations',
      path: '/settings/:tab',
    });
  }

  test('connecting YouTube submits the API key (not a bare toggle)', async () => {
    const user = userEvent.setup();
    const repo = new FakePanelRepository(buildPanelState());
    renderIntegrations(repo);

    // youtube is seeded disconnected → its card shows the API-key form.
    const keyInput = await screen.findByLabelText(/YouTube Data API key/i);
    await user.type(keyInput, 'AIza-customer-key');
    await user.click(screen.getByRole('button', { name: 'Connect' }));

    await waitFor(() => {
      expect(repo.youtubeConnects).toEqual(['AIza-customer-key']);
    });
  });

  test('Telegram wizard records the phone then the verify code', async () => {
    const user = userEvent.setup();
    const repo = new FakePanelRepository(buildPanelState());
    renderIntegrations(repo);

    await user.type(await screen.findByLabelText(/Phone number/i), '+14155550142');
    await user.click(screen.getByRole('button', { name: 'Send code' }));

    await waitFor(() => {
      expect(repo.telegramStarts).toEqual(['+14155550142']);
    });

    // start succeeded → the code step appears
    await user.type(await screen.findByLabelText(/Login code/i), '54321');
    await user.click(screen.getByRole('button', { name: 'Verify' }));

    await waitFor(() => {
      expect(repo.telegramVerifies).toHaveLength(1);
    });
    expect(repo.telegramVerifies[0]).toMatchObject({ token: 'tg-wizard-token', code: '54321' });
  });

  test('Telegram wizard reveals the 2FA password field when required', async () => {
    const user = userEvent.setup();
    const repo = new FakePanelRepository(buildPanelState());
    repo.telegramNeedsPassword = true;
    renderIntegrations(repo);

    await user.type(await screen.findByLabelText(/Phone number/i), '+14155550142');
    await user.click(screen.getByRole('button', { name: 'Send code' }));
    await user.type(await screen.findByLabelText(/Login code/i), '54321');
    await user.click(screen.getByRole('button', { name: 'Verify' }));

    // first verify → 2FA required → the password field appears
    const password = await screen.findByLabelText(/Two-factor password/i);
    await user.type(password, 'hunter2');
    await user.click(screen.getByRole('button', { name: 'Verify' }));

    await waitFor(() => {
      expect(repo.telegramVerifies).toHaveLength(2);
    });
    expect(repo.telegramVerifies[1]).toMatchObject({ code: '54321', password: 'hunter2' });
  });

  test('Instagram is shown as managed, not self-serve', async () => {
    const repo = new FakePanelRepository(buildPanelState());
    renderIntegrations(repo);
    expect(await screen.findByText(/Managed by your administrator/i)).toBeInTheDocument();
  });

  test('X and LinkedIn are shown as managed (CDP, no self-serve connect)', async () => {
    const repo = new FakePanelRepository(
      buildPanelState({
        INTEGRATIONS: [
          buildIntegration({ id: 'x', platform: 'x', name: 'X', connected: true, detail: 'Connected' }),
          buildIntegration({ id: 'linkedin', platform: 'linkedin', name: 'LinkedIn', connected: true, detail: 'Connected' }),
        ],
      }),
    );
    renderIntegrations(repo);
    // Both are managed-CDP (warmed Chrome, no per-org secret) → managed card, no form.
    expect(await screen.findAllByText(/Managed by your administrator/i)).toHaveLength(2);
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
  });

  test('a needs-reconnect YouTube connection shows the badge and re-opens the form', async () => {
    const repo = new FakePanelRepository(
      buildPanelState({
        INTEGRATIONS: [
          buildIntegration({
            id: 'youtube', platform: 'youtube', name: 'Youtube',
            connected: true, detail: 'needs reconnect',
          }),
        ],
      }),
    );
    renderIntegrations(repo);

    // even though connected=true, the engine flagged it → reconnect badge + form
    expect(await screen.findByText(/Needs reconnect/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/YouTube Data API key/i)).toBeInTheDocument();
  });
});

/** Render the new-campaign wizard and skip straight to the manual form (the
 *  AI composer is Step 1; "Start from scratch" lands on the blank create form). */
async function renderManualNewCampaign(
  user: ReturnType<typeof userEvent.setup>,
  repo: FakePanelRepository,
) {
  renderWithProviders(<CampaignNewPage />, {
    repository: repo,
    route: '/campaigns/new',
    path: '/campaigns/new',
  });
  await user.click(await screen.findByRole('button', { name: /start from scratch/i }));
}

describe('Campaign creation', () => {
  test('submitting the new-campaign form records a draft campaign', async () => {
    const user = userEvent.setup();
    const repo = new FakePanelRepository(buildPanelState());
    await renderManualNewCampaign(user, repo);

    await user.type(screen.getByPlaceholderText(/summer launch/i), 'Spring Drive');
    await user.selectOptions(screen.getByLabelText('Platform'), 'youtube');
    await user.type(screen.getByLabelText(/Relevance — what counts/), 'saas app demo');
    // YouTube needs a seed (search query or channel id) before it can be saved.
    await user.type(screen.getByLabelText(/search queries/i), 'project management saas');
    await user.click(screen.getByRole('button', { name: /save draft/i }));

    await waitFor(() => {
      expect(repo.campaignCreates).toHaveLength(1);
    });
    const sent = repo.campaignCreates[0];
    expect(sent?.displayName).toBe('Spring Drive');
    expect(sent?.status).toBe('draft');
    // The brief is captured and travels with the create.
    expect(sent?.brief?.platform).toBe('youtube');
    expect(sent?.brief?.relevanceDef).toBe('saas app demo');
  });

  test('advanced classifier prompts travel with the brief when filled', async () => {
    const user = userEvent.setup();
    const repo = new FakePanelRepository(buildPanelState());
    await renderManualNewCampaign(user, repo);

    await user.type(screen.getByPlaceholderText(/summer launch/i), 'Prompted');
    // Instagram (default) needs no seed, so the form is saveable as-is.
    await user.type(screen.getByLabelText(/Match prompt/), 'CUSTOM MATCH PROMPT');
    await user.click(screen.getByRole('button', { name: /save draft/i }));

    await waitFor(() => {
      expect(repo.campaignCreates).toHaveLength(1);
    });
    expect(repo.campaignCreates[0]?.brief?.matchPrompt).toBe('CUSTOM MATCH PROMPT');
    // Left-blank prompts are sent empty (the server treats blank as "keep existing").
    expect(repo.campaignCreates[0]?.brief?.visionPrompt).toBe('');
  });

  // Regression: the numeric inputs must accept arbitrary in-range values. A
  // `step` grid (e.g. step=100) makes the browser reject off-grid values with
  // a native "Please enter a valid value" bubble that silently blocks submit —
  // so a budget of 5250, a goal of 175, or a threshold of 0.72 would never
  // reach the server. The defaults happen to sit on the old grid, hiding it.
  test('accepts off-grid budget, goal and threshold values', async () => {
    const user = userEvent.setup();
    const repo = new FakePanelRepository(buildPanelState());
    await renderManualNewCampaign(user, repo);

    await user.type(screen.getByPlaceholderText(/summer launch/i), 'Odd Numbers');
    const budget = screen.getByLabelText(/Budget cap/i);
    const goal = screen.getByLabelText(/goal target/i);
    const threshold = screen.getByLabelText(/Match threshold/i);
    await user.clear(budget);
    await user.type(budget, '5250');
    await user.clear(goal);
    await user.type(goal, '175');
    await user.clear(threshold);
    await user.type(threshold, '0.72');

    // No constraint violation — the browser would otherwise block submission.
    expect((budget as HTMLInputElement).validity.valid).toBe(true);
    expect((goal as HTMLInputElement).validity.valid).toBe(true);
    expect((threshold as HTMLInputElement).validity.valid).toBe(true);

    await user.click(screen.getByRole('button', { name: /save draft/i }));

    await waitFor(() => {
      expect(repo.campaignCreates).toHaveLength(1);
    });
    const sent = repo.campaignCreates[0];
    expect(sent?.budgetCap).toBe(5250);
    expect(sent?.goalTarget).toBe(175);
    expect(sent?.brief?.threshold).toBe(0.72);
  });
});

describe('Platform-aware campaign form', () => {
  // Skip the AI composer (Step 1) → the manual create form (Step 2).
  async function renderNew(user: ReturnType<typeof userEvent.setup>, repo: FakePanelRepository) {
    await renderManualNewCampaign(user, repo);
  }

  test('Telegram shows a channels field, hides Instagram seeds, and requires channels', async () => {
    const user = userEvent.setup();
    const repo = new FakePanelRepository(buildPanelState());
    await renderNew(user, repo);

    await user.type(screen.getByPlaceholderText(/summer launch/i), 'Acme TG');
    await user.selectOptions(screen.getByLabelText('Platform'), 'telegram');

    // Instagram-only seeds are gone; the Telegram channels field is present.
    expect(screen.queryByLabelText(/seed accounts/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/seed hashtags/i)).not.toBeInTheDocument();
    const channels = screen.getByLabelText(/channels to monitor/i);

    // Save is blocked until channels are supplied — Telegram can't run without them.
    const save = screen.getByRole('button', { name: /save draft/i });
    expect(save).toBeDisabled();

    await user.type(channels, '@product_chat, @acme_demos');
    expect(save).toBeEnabled();
    await user.click(save);

    await waitFor(() => { expect(repo.campaignCreates).toHaveLength(1); });
    const sent = repo.campaignCreates[0];
    expect(sent?.brief?.platform).toBe('telegram');
    expect(sent?.brief?.seedChannels).toEqual(['@product_chat', '@acme_demos']);
  });

  test('YouTube exposes search queries and channel IDs and accepts either', async () => {
    const user = userEvent.setup();
    const repo = new FakePanelRepository(buildPanelState());
    await renderNew(user, repo);

    await user.type(screen.getByPlaceholderText(/summer launch/i), 'YT Leads');
    await user.selectOptions(screen.getByLabelText('Platform'), 'youtube');

    expect(screen.getByLabelText(/search queries/i)).toBeInTheDocument();
    const channelIds = screen.getByLabelText(/channel ids/i);
    expect(screen.queryByLabelText(/seed accounts/i)).not.toBeInTheDocument();

    // A channel id alone satisfies the "at least one seed" rule.
    await user.type(channelIds, 'UCabc123');
    await user.click(screen.getByRole('button', { name: /save draft/i }));

    await waitFor(() => { expect(repo.campaignCreates).toHaveLength(1); });
    expect(repo.campaignCreates[0]?.brief?.seedChannels).toEqual(['UCabc123']);
  });

  test('Instagram shows hashtags + accounts and can save with no seeds (home feed)', async () => {
    const user = userEvent.setup();
    const repo = new FakePanelRepository(buildPanelState());
    await renderNew(user, repo);

    await user.type(screen.getByPlaceholderText(/summer launch/i), 'IG Leads');
    // Default platform is instagram.
    expect(screen.getByLabelText(/seed hashtags/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/seed accounts/i)).toBeInTheDocument();

    // No seeds required — Instagram also walks the home feed.
    await user.click(screen.getByRole('button', { name: /save draft/i }));
    await waitFor(() => { expect(repo.campaignCreates).toHaveLength(1); });
    expect(repo.campaignCreates[0]?.brief?.platform).toBe('instagram');
  });

  test('X exposes saved-searches + accounts, saves with no seeds, and carries them when filled', async () => {
    const user = userEvent.setup();
    const repo = new FakePanelRepository(buildPanelState());
    await renderNew(user, repo);

    await user.type(screen.getByPlaceholderText(/summer launch/i), 'X Leads');
    await user.selectOptions(screen.getByLabelText('Platform'), 'x');

    // Managed-CDP with a For You feed → seeds optional, channels field hidden.
    expect(screen.queryByLabelText(/channels to monitor/i)).not.toBeInTheDocument();
    await user.type(screen.getByLabelText(/saved searches \/ hashtags/i), 'saas projectmanagement');
    await user.type(screen.getByLabelText(/accounts \/ list members/i), '@acme, @devuz');
    await user.click(screen.getByRole('button', { name: /save draft/i }));

    await waitFor(() => { expect(repo.campaignCreates).toHaveLength(1); });
    const sent = repo.campaignCreates[0];
    expect(sent?.brief?.platform).toBe('x');
    expect(sent?.brief?.seedHashtags).toEqual(['saas projectmanagement']);
    expect(sent?.brief?.seedAccounts).toEqual(['@acme', '@devuz']);
  });

  test('LinkedIn exposes hashtags + people/companies and carries them on the brief', async () => {
    const user = userEvent.setup();
    const repo = new FakePanelRepository(buildPanelState());
    await renderNew(user, repo);

    await user.type(screen.getByPlaceholderText(/summer launch/i), 'LI Leads');
    await user.selectOptions(screen.getByLabelText('Platform'), 'linkedin');

    await user.type(screen.getByLabelText(/seed hashtags/i), 'projectmanagement, saas');
    await user.type(screen.getByLabelText(/people \/ companies/i), 'in/jane-doe');
    // No requireAnyOf — LinkedIn also walks the home feed, so it saves regardless.
    await user.click(screen.getByRole('button', { name: /save draft/i }));

    await waitFor(() => { expect(repo.campaignCreates).toHaveLength(1); });
    const sent = repo.campaignCreates[0];
    expect(sent?.brief?.platform).toBe('linkedin');
    expect(sent?.brief?.seedHashtags).toEqual(['projectmanagement', 'saas']);
    expect(sent?.brief?.seedAccounts).toEqual(['in/jane-doe']);
  });
});

describe('Campaign editing', () => {
  test('editing budget and saving upserts with the id fixed and status preserved', async () => {
    const user = userEvent.setup();
    const repo = new FakePanelRepository(
      buildPanelState({
        CAMPAIGNS: [buildCampaign({
          id: 'acme-leadgen', name: 'Acme Leadgen',
          status: 'live', goalType: 'lead', budgetCap: 5000, goalTarget: 100,
          briefForm: {
            platform: 'instagram', goal: 'lead', threshold: 0.7,
            languageMix: ['en'], relevanceDef: 'saas product',
            matchDef: 'buyer intent', extractDef: '- phone',
            relevancePrompt: '', matchPrompt: '', visionPrompt: '',
            seedHashtags: ['projectmanagement'], seedAccounts: [], seedChannels: [],
          },
        })],
      }),
    );
    renderWithProviders(<CampaignEditPage />, {
      repository: repo,
      route: '/campaigns/acme-leadgen/edit',
      path: '/campaigns/:campaignId/edit',
    });

    // The form seeds from the loaded campaign, brief included.
    expect(await screen.findByDisplayValue('Acme Leadgen')).toBeInTheDocument();
    expect(screen.getByDisplayValue('saas product')).toBeInTheDocument();
    const budget = screen.getByDisplayValue('5000');
    await user.clear(budget);
    await user.type(budget, '8000');
    await user.selectOptions(screen.getByLabelText('Platform'), 'telegram');
    // Telegram has no algorithmic feed, so it requires channels to monitor.
    await user.type(screen.getByLabelText(/channels to monitor/i), '@product_chat');
    await user.click(screen.getByRole('button', { name: /save changes/i }));

    await waitFor(() => {
      expect(repo.campaignCreates).toHaveLength(1);
    });
    const sent = repo.campaignCreates[0];
    expect(sent?.campaignId).toBe('acme-leadgen');     // engine id stays fixed
    expect(sent?.status).toBe('live');                 // status preserved, not reset to draft
    expect(sent?.budgetCap).toBe(8000);                // the edit
    expect(sent?.brief?.platform).toBe('telegram');    // brief edit travels too
    expect(sent?.brief?.relevanceDef).toBe('saas product'); // seeded value preserved
  });
});

describe('Campaign activation', () => {
  test('a draft with a brief shows Activate and clicking it goes live', async () => {
    const user = userEvent.setup();
    const repo = new FakePanelRepository(
      buildPanelState({
        CAMPAIGNS: [buildCampaign({ id: 'cmp-001', status: 'draft', briefForm: RUNNABLE_BRIEF })],
      }),
    );
    renderWithProviders(<CampaignsPage />, { repository: repo });

    await user.click(await screen.findByRole('button', { name: /activate/i }));

    await waitFor(() => { expect(repo.campaignCreates).toHaveLength(1); });
    // A partial upsert: just the id + the new status (the bridge COALESCEs the rest).
    expect(repo.campaignCreates[0]).toMatchObject({ campaignId: 'cmp-001', status: 'live' });
  });

  test('a draft without a brief offers no Activate (it could not run live)', async () => {
    const repo = new FakePanelRepository(
      buildPanelState({
        CAMPAIGNS: [buildCampaign({ status: 'draft', briefForm: null })],
      }),
    );
    renderWithProviders(<CampaignsPage />, { repository: repo });

    // The card still renders (Edit draft proves it), but there's nothing to activate.
    expect(await screen.findByText('Edit draft')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /activate/i })).not.toBeInTheDocument();
  });
});

describe('Campaign run', () => {
  test('hides the Run button for a campaign that is not runnable', async () => {
    const repo = new FakePanelRepository(
      buildPanelState({ CAMPAIGNS: [buildCampaign({ status: 'draft', briefForm: null })] }),
    );
    renderWithProviders(<CampaignsPage />, { repository: repo });

    // The card renders (Edit draft proves it), but there's no Run affordance.
    expect(await screen.findByText('Edit draft')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Run' })).not.toBeInTheDocument();
  });

  test('starting a run from the drawer sends a live run with the chosen lead target', async () => {
    const user = userEvent.setup();
    const repo = new FakePanelRepository(
      buildPanelState({ CAMPAIGNS: [runnableCampaign({ id: 'cmp-001', goalTarget: null })] }),
    );
    // v27: the default Free fixture caps a run at 10 leads (7 remaining). This test is
    // about the target the drawer SENDS, not about the clamp, so put the org on a plan
    // roomy enough that no clamp fires.
    repo.billing = buildBilling({ tier: 'pro', leadCap: 2000, leadsUsed: 0, maxRunLeads: 2000, campaignCap: null, campaignsUsed: 1 });
    renderWithProviders(<CampaignsPage />, { repository: repo });

    await user.click(await screen.findByRole('button', { name: 'Run' }));
    await user.click(screen.getByRole('button', { name: '50' }));
    await user.click(screen.getByRole('button', { name: 'Start run' }));

    await waitFor(() => { expect(repo.runRequests).toHaveLength(1); });
    expect(repo.runRequests[0]).toEqual({
      campaignId: 'cmp-001', mode: 'live', targetLeadCount: 50, durationMinutes: 120,
    });
  });

  test('a custom lead target is sent as entered, with the safety cap', async () => {
    const user = userEvent.setup();
    const repo = new FakePanelRepository(
      buildPanelState({ CAMPAIGNS: [runnableCampaign({ id: 'cmp-001', goalTarget: null })] }),
    );
    // v27: the default Free fixture caps a run at 10 leads (7 remaining). This test is
    // about the target the drawer SENDS, not about the clamp, so put the org on a plan
    // roomy enough that no clamp fires.
    repo.billing = buildBilling({ tier: 'pro', leadCap: 2000, leadsUsed: 0, maxRunLeads: 2000, campaignCap: null, campaignsUsed: 1 });
    renderWithProviders(<CampaignsPage />, { repository: repo });

    await user.click(await screen.findByRole('button', { name: 'Run' }));
    await user.click(screen.getByRole('button', { name: 'Custom lead target' }));
    const leadsField = screen.getByLabelText(/Leads/);
    await user.clear(leadsField);
    await user.type(leadsField, '37');
    const capField = screen.getByLabelText(/Safety cap/);
    await user.clear(capField);
    await user.type(capField, '90');
    await user.click(screen.getByRole('button', { name: 'Start run' }));

    await waitFor(() => { expect(repo.runRequests).toHaveLength(1); });
    expect(repo.runRequests[0]).toEqual({
      campaignId: 'cmp-001', mode: 'live', targetLeadCount: 37, durationMinutes: 90,
    });
  });

  test('the lead target pre-fills from the campaign goal when set', async () => {
    const user = userEvent.setup();
    const repo = new FakePanelRepository(
      buildPanelState({ CAMPAIGNS: [runnableCampaign({ id: 'cmp-001', goalTarget: 200 })] }),
    );
    // v27: the default Free fixture caps a run at 10 leads (7 remaining). This test is
    // about the target the drawer SENDS, not about the clamp, so put the org on a plan
    // roomy enough that no clamp fires.
    repo.billing = buildBilling({ tier: 'pro', leadCap: 2000, leadsUsed: 0, maxRunLeads: 2000, campaignCap: null, campaignsUsed: 1 });
    renderWithProviders(<CampaignsPage />, { repository: repo });

    await user.click(await screen.findByRole('button', { name: 'Run' }));
    // 200 isn't a preset → the drawer opens on Custom, pre-filled with the goal.
    expect(screen.getByLabelText(/Leads/)).toHaveValue(200);
    await user.click(screen.getByRole('button', { name: 'Start run' }));

    await waitFor(() => { expect(repo.runRequests).toHaveLength(1); });
    expect(repo.runRequests[0]).toEqual({
      campaignId: 'cmp-001', mode: 'live', targetLeadCount: 200, durationMinutes: 120,
    });
  });

  test('the card shows Running and the drawer can stop an in-flight run', async () => {
    const user = userEvent.setup();
    const repo = new FakePanelRepository(
      buildPanelState({
        CAMPAIGNS: [runnableCampaign({ id: 'cmp-001' })],
        RUN: buildRunBlock({ active: buildActiveRun({ scope: 'campaign', campaignId: 'cmp-001' }) }),
      }),
    );
    renderWithProviders(<CampaignsPage />, { repository: repo });

    await user.click(await screen.findByRole('button', { name: 'Running…' }));
    await user.click(screen.getByRole('button', { name: 'Stop run' }));

    await waitFor(() => { expect(repo.stopRequests).toBe(1); });
  });

  test('the drawer streams the live activity feed for an in-flight run', async () => {
    const user = userEvent.setup();
    const repo = new FakePanelRepository(
      buildPanelState({
        CAMPAIGNS: [runnableCampaign({ id: 'cmp-001' })],
        RUN: buildRunBlock({
          active: buildActiveRun({ id: 'run-001', scope: 'campaign', campaignId: 'cmp-001' }),
        }),
      }),
    );
    repo.runActivity = buildRunActivity({
      runId: 'run-001', phase: 'searching', leadsFound: 2, targetLeads: 10,
    });
    renderWithProviders(<CampaignsPage />, { repository: repo });

    await user.click(await screen.findByRole('button', { name: 'Running…' }));

    // v27: the progress SCALARS render from the polled feed, proving it's wired into the
    // running drawer. The narrative log is a superadmin surface now (B3).
    expect(await screen.findByText('of 10 leads')).toBeInTheDocument();
    expect(screen.getByText('Searching for posts')).toBeInTheDocument();
    expect(repo.runActivityFetches[0]).toEqual({ runId: 'run-001', afterSeq: 0 });
  });

  test('a fleet-routed run streams its live feed by the returned runId', async () => {
    // Gap A: a fleet run does NOT populate the in-process RUN block, so the drawer
    // polls the runId the start response returned and shows the synced live activity.
    const user = userEvent.setup();
    const repo = new FakePanelRepository(
      buildPanelState({ CAMPAIGNS: [runnableCampaign({ id: 'cmp-001' })] }),
    );
    repo.nextRunStart = { runId: 'run-fleet-1', backend: 'distributed' };
    repo.runActivity = buildRunActivity({
      runId: 'run-fleet-1', phase: 'searching', leadsFound: 1, targetLeads: 10,
    });
    renderWithProviders(<CampaignsPage />, { repository: repo });

    await user.click(await screen.findByRole('button', { name: 'Run' }));
    await user.click(screen.getByRole('button', { name: 'Start run' }));

    expect(await screen.findByText('of 10 leads')).toBeInTheDocument();
    // FIX 2: a live fleet run now renders in the live view (not the idle "last run"
    // block) — its activity reports finished=false, so the drawer treats it as in
    // flight and shows the fleet-managed hint instead of Stop/Pause controls.
    expect(screen.getByText(/executing on a worker in your fleet/i)).toBeInTheDocument();
    await waitFor(() => {
      expect(repo.runActivityFetches.some((f) => f.runId === 'run-fleet-1')).toBe(true);
    });
  });

  test('an in-process run with a stale fleetRunId still shows Stop (not the fleet view)', async () => {
    // Regression: an in-process run for a campaign that ALSO carries a leftover
    // fleetRunId (e.g. an old queued fleet job) must keep Stop/Pause and must NOT be
    // mislabeled as "executing on a worker" — the process-global RUN lock wins.
    const user = userEvent.setup();
    const repo = new FakePanelRepository(
      buildPanelState({
        CAMPAIGNS: [runnableCampaign({ id: 'cmp-001', fleetRunId: 'stale-fleet-run' })],
        RUN: buildRunBlock({
          active: buildActiveRun({ id: 'run-inproc', scope: 'campaign', campaignId: 'cmp-001' }),
        }),
      }),
    );
    renderWithProviders(<CampaignsPage />, { repository: repo });

    await user.click(await screen.findByRole('button', { name: 'Running…' }));

    expect(screen.getByRole('button', { name: 'Stop run' })).toBeInTheDocument();
    expect(screen.queryByText(/executing on a worker in your fleet/i)).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Stop run' }));
    await waitFor(() => { expect(repo.stopRequests).toBe(1); });
  });

  test('starting a run does not leave the drawer stuck in a loading spinner', async () => {
    const user = userEvent.setup();
    const repo = new FakePanelRepository(
      buildPanelState({ CAMPAIGNS: [runnableCampaign({ id: 'cmp-001' })] }),
    );
    renderWithProviders(<CampaignsPage />, { repository: repo });

    await user.click(await screen.findByRole('button', { name: 'Run' }));
    await user.click(screen.getByRole('button', { name: 'Start run' }));
    await waitFor(() => { expect(repo.runRequests).toHaveLength(1); });

    // Regression: the old isSuccess-driven spinner never reset, so the Start
    // control stayed disabled/spinning forever once a run was accepted. It must
    // return to interactive after the request settles.
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Start run' })).toBeEnabled();
    });
  });

  test('an idle campaign drawer shows the last run activity (not only live runs)', async () => {
    const user = userEvent.setup();
    const repo = new FakePanelRepository(
      buildPanelState({
        CAMPAIGNS: [runnableCampaign({ id: 'cmp-001' })],
        RUN: buildRunBlock({
          active: null, // nothing running now
          recent: [buildRunRecord({ id: 'run-009', campaignId: 'cmp-001', outcome: 'ok', summary: 'matches 2' })],
        }),
      }),
    );
    repo.runActivity = buildRunActivity({
      runId: 'run-009',
      finished: true,
      phase: 'done', leadsFound: 2, leadsDelivered: 2, delivery: 'delivered', targetLeads: 10,
    });
    renderWithProviders(<CampaignsPage />, { repository: repo });

    // The card is idle → button says "Run"; opening it still surfaces the last run.
    await user.click(await screen.findByRole('button', { name: 'Run' }));

    expect(await screen.findByText('of 10 leads')).toBeInTheDocument();
    expect(screen.getByText('Last run')).toBeInTheDocument();
    expect(repo.runActivityFetches[0]).toEqual({ runId: 'run-009', afterSeq: 0 });
  });
});

import { buildLeadNote } from '@/test/fixtures';

describe('Lead drawer notes', () => {
  test('adding a note records a note write for the lead', async () => {
    const user = userEvent.setup();
    const lead = buildMatch({ commentId: 'c1' });
    const repo = new FakePanelRepository(buildPanelState({ MATCHES: [lead] }));
    renderWithProviders(<LeadsPage />, { repository: repo, route: leadRoute(lead.id), path: '/leads/:leadId' });

    const textarea = await screen.findByPlaceholderText('Add a note…');
    await user.type(textarea, 'Left a voicemail');
    await user.click(screen.getByRole('button', { name: 'Add note' }));

    await waitFor(() => { expect(repo.noteAdds).toHaveLength(1); });
    expect(repo.noteAdds[0]?.body).toBe('Left a voicemail');
    expect(repo.noteAdds[0]?.commentId).toBe('c1');
  });

  test('only the author sees a delete control on a note', async () => {
    const lead = buildMatch({
      commentId: 'c1',
      notes: [
        buildLeadNote({ id: '10', authorId: 1, createdAt: 'Jun 11' }), // current user
        buildLeadNote({ id: '11', authorId: 99, createdAt: 'Jun 12' }), // someone else
      ],
    });
    const repo = new FakePanelRepository(buildPanelState({ MATCHES: [lead] }));
    renderWithProviders(<LeadsPage />, { repository: repo, route: leadRoute(lead.id), path: '/leads/:leadId' });

    expect(await screen.findByRole('button', { name: 'Delete note from Jun 11' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Delete note from Jun 12' })).toBeNull();
  });
});
