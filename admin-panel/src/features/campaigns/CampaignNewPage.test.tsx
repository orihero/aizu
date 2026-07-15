import { describe, expect, test } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { buildPanelState } from '@/test/fixtures';
import { FakePanelRepository } from '@/test/fakePanelRepository';
import { renderWithProviders } from '@/test/renderWithProviders';
import type { InterviewResponse } from '@/shared/types/domain';
import { CampaignNewPage } from './CampaignNewPage';

function renderWizard(repo: FakePanelRepository) {
  renderWithProviders(<CampaignNewPage />, {
    repository: repo,
    route: '/campaigns/new',
    path: '/campaigns/new',
  });
}

/** A round-1 reply with a single-choice goal question + a platforms picker. */
function firstRound(): InterviewResponse {
  return {
    done: false,
    round: 1,
    productContext: 'PRODUCT DESCRIPTION:\nsaas app',
    questions: [
      {
        id: 'goal',
        type: 'single',
        prompt: 'What is your primary goal?',
        options: [
          { value: 'lead', label: 'Find leads' },
          { value: 'traffic', label: 'Drive traffic' },
        ],
      },
      { id: 'platforms', type: 'platforms', prompt: 'Which platforms?', suggested: ['instagram'] },
    ],
  };
}

const DONE_ROUND: InterviewResponse = { done: true, round: 2, productContext: 'PRODUCT DESCRIPTION:\nsaas app', questions: [] };

async function startInterview(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText(/what are you promoting/i), 'saas app');
  await user.click(screen.getByRole('button', { name: /continue/i }));
}

describe('CampaignNewPage — AI interview wizard', () => {
  test('the Continue CTA is disabled until at least one input is provided', async () => {
    const user = userEvent.setup();
    const repo = new FakePanelRepository(buildPanelState());
    renderWizard(repo);

    const cta = screen.getByRole('button', { name: /continue/i });
    expect(cta).toBeDisabled();

    await user.type(screen.getByLabelText(/what are you promoting/i), 'Acme app demo');
    expect(cta).toBeEnabled();
  });

  test('intro launches the interview; answering a round then continuing drafts the brief', async () => {
    const user = userEvent.setup();
    const repo = new FakePanelRepository(buildPanelState());
    repo.interviewResults = [firstRound(), DONE_ROUND];
    repo.generatedDraft = {
      name: 'Acme SaaS Lead Gen',
      platform: 'instagram',
      relevanceDef: 'teams evaluating project-management software',
    };
    renderWizard(repo);

    await startInterview(user);

    // The interview round renders the AI's questions.
    expect(await screen.findByText(/what is your primary goal/i)).toBeInTheDocument();
    await user.click(screen.getByRole('radio', { name: /find leads/i }));
    await user.click(screen.getByRole('button', { name: /^continue$/i }));

    // Converges (round 2 is done) → synthesizes → prefilled review form.
    expect(await screen.findByText(/drafted by ai/i)).toBeInTheDocument();
    expect(screen.getByDisplayValue('Acme SaaS Lead Gen')).toBeInTheDocument();

    // Round 1 used the intro text; round 2 echoed the productContext + transcript.
    expect(repo.interviewRequests[0]).toMatchObject({ text: 'saas app', round: 1 });
    expect(repo.interviewRequests[1]).toMatchObject({ round: 2, productContext: 'PRODUCT DESCRIPTION:\nsaas app' });
    expect(repo.interviewRequests[1]?.interview).toEqual([
      { question: 'What is your primary goal?', answer: 'Find leads' },
      { question: 'Which platforms?', answer: 'Instagram' },
    ]);
    // Synthesis carried the context, transcript, and chosen platforms.
    const gen = repo.generateRequests[0];
    expect(gen?.productContext).toBe('PRODUCT DESCRIPTION:\nsaas app');
    expect(gen?.platforms).toEqual(['instagram']);
    expect(gen?.interview).toHaveLength(2);
  });

  test('the user can add a platform the AI did not suggest', async () => {
    const user = userEvent.setup();
    const repo = new FakePanelRepository(buildPanelState());
    repo.interviewResults = [firstRound()];   // round 2 falls back to done
    repo.generatedDraft = { name: 'Multi Platform' };
    renderWizard(repo);

    await startInterview(user);
    await screen.findByText(/which platforms/i);

    // Instagram is pre-selected (suggested); add X, then finish early.
    await user.click(screen.getByRole('button', { name: /^X$/ }));
    await user.click(screen.getByRole('radio', { name: /find leads/i }));
    await user.click(screen.getByRole('button', { name: /skip the rest/i }));

    await screen.findByText(/drafted by ai/i);
    expect(repo.generateRequests[0]?.platforms).toEqual(['instagram', 'x']);
    // Finishing early skipped the extra interview round.
    expect(repo.interviewRequests).toHaveLength(1);
  });

  test('Skip the rest drafts immediately without another interview round', async () => {
    const user = userEvent.setup();
    const repo = new FakePanelRepository(buildPanelState());
    repo.interviewResults = [firstRound()];
    repo.generatedDraft = { name: 'Quick Draft' };
    renderWizard(repo);

    await startInterview(user);
    await screen.findByText(/what is your primary goal/i);
    await user.click(screen.getByRole('button', { name: /skip the rest/i }));

    expect(await screen.findByDisplayValue('Quick Draft')).toBeInTheDocument();
    expect(repo.interviewRequests).toHaveLength(1);
    expect(repo.generateRequests).toHaveLength(1);
  });

  test('saving from the review records a draft create with the edited values', async () => {
    const user = userEvent.setup();
    const repo = new FakePanelRepository(buildPanelState());
    repo.interviewResults = [DONE_ROUND];   // AI is immediately confident
    repo.generatedDraft = { name: 'Draft One', platform: 'instagram' };
    renderWizard(repo);

    await startInterview(user);

    await screen.findByDisplayValue('Draft One');
    await user.click(screen.getByRole('button', { name: /save draft/i }));

    await waitFor(() => { expect(repo.campaignCreates).toHaveLength(1); });
    expect(repo.campaignCreates[0]?.displayName).toBe('Draft One');
    expect(repo.campaignCreates[0]?.status).toBe('draft');
  });

  test('Start from scratch skips to a blank form with no AI banner', async () => {
    const user = userEvent.setup();
    const repo = new FakePanelRepository(buildPanelState());
    renderWizard(repo);

    await user.click(screen.getByRole('button', { name: /start from scratch/i }));

    expect(screen.getByLabelText(/campaign name/i)).toBeInTheDocument();
    expect(screen.queryByText(/drafted by ai/i)).not.toBeInTheDocument();
    expect(repo.interviewRequests).toHaveLength(0);
    expect(repo.generateRequests).toHaveLength(0);
  });

  test('a no-AI-key (503) failure on the first round shows a retryable panel with manual fill', async () => {
    const user = userEvent.setup();
    const repo = new FakePanelRepository(buildPanelState());
    repo.interviewFailure = 'keyMissing';
    renderWizard(repo);

    await startInterview(user);

    expect(await screen.findByRole('alert')).toHaveTextContent(/AI drafting is unavailable/i);
    await user.click(screen.getByRole('button', { name: /fill it in manually/i }));
    expect(screen.getByLabelText(/campaign name/i)).toBeInTheDocument();
  });

  test('a single-platform AI draft prefills the unified platform editor (no separate dropdown)', async () => {
    const user = userEvent.setup();
    const repo = new FakePanelRepository(buildPanelState());
    repo.interviewResults = [DONE_ROUND];
    repo.generatedDraft = { name: 'X Campaign', platform: 'x' };
    renderWizard(repo);

    await startInterview(user);
    await screen.findByDisplayValue('X Campaign');

    // One "Platform" control, prefilled with the chosen platform.
    const selects = screen.getAllByLabelText<HTMLSelectElement>('Platform');
    expect(selects).toHaveLength(1);
    expect(selects[0]?.value).toBe('x');
  });

  test('a multi-platform AI draft prefills one card per chosen platform', async () => {
    const user = userEvent.setup();
    const repo = new FakePanelRepository(buildPanelState());
    repo.interviewResults = [DONE_ROUND];
    repo.generatedDraft = {
      name: 'Multi',
      platform: 'x',
      channels: [
        { platform: 'x', seedHashtags: 'saas', seedAccounts: '', seedChannels: '' },
        { platform: 'reddit', seedHashtags: '', seedAccounts: '', seedChannels: 'r/saas' },
      ],
    };
    renderWizard(repo);

    await startInterview(user);
    await screen.findByDisplayValue('Multi');

    const selects = screen.getAllByLabelText<HTMLSelectElement>('Platform');
    expect(selects.map((s) => s.value)).toEqual(['x', 'reddit']);
  });

  test('AI-generated advanced classifier prompts prefill and persist on save', async () => {
    const user = userEvent.setup();
    const repo = new FakePanelRepository(buildPanelState());
    repo.interviewResults = [DONE_ROUND];
    repo.generatedDraft = {
      name: 'With Prompts',
      platform: 'instagram',
      relevancePrompt: 'TUNED RELEVANCE PROMPT',
      matchPrompt: 'TUNED MATCH PROMPT',
      visionPrompt: 'TUNED VISION PROMPT',
    };
    renderWizard(repo);

    await startInterview(user);
    await screen.findByDisplayValue('With Prompts');

    // The Advanced section is prefilled (the textareas exist even while collapsed).
    expect(screen.getByDisplayValue('TUNED RELEVANCE PROMPT')).toBeInTheDocument();
    expect(screen.getByDisplayValue('TUNED MATCH PROMPT')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /save draft/i }));
    await waitFor(() => { expect(repo.campaignCreates).toHaveLength(1); });
    // Non-blank prompts are sent on the brief (server persists them, not "leave as-is").
    expect(repo.campaignCreates[0]?.brief?.relevancePrompt).toBe('TUNED RELEVANCE PROMPT');
    expect(repo.campaignCreates[0]?.brief?.matchPrompt).toBe('TUNED MATCH PROMPT');
    expect(repo.campaignCreates[0]?.brief?.visionPrompt).toBe('TUNED VISION PROMPT');
  });

  test('Regenerate returns to the composer with the original inputs intact', async () => {
    const user = userEvent.setup();
    const repo = new FakePanelRepository(buildPanelState());
    repo.interviewResults = [DONE_ROUND];
    repo.generatedDraft = { name: 'First Draft' };
    renderWizard(repo);

    await user.type(screen.getByLabelText(/what are you promoting/i), 'my product pitch');
    await user.click(screen.getByRole('button', { name: /continue/i }));

    await screen.findByText(/drafted by ai/i);
    await user.click(screen.getByRole('button', { name: /regenerate/i }));

    expect(screen.getByLabelText(/what are you promoting/i)).toHaveValue('my product pitch');
  });
});
