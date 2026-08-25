import { useState } from 'react';
import { describe, expect, test } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { Role } from '@/shared/auth/roles';
import type { Match } from '@/shared/types/domain';
import { FakePanelRepository } from '@/test/fakePanelRepository';
import { buildMatch, buildPanelState } from '@/test/fixtures';
import { renderWithProviders } from '@/test/renderWithProviders';
import { LEAD_INTENT_PLACEHOLDER } from '@/shared/selectors/leads';
import { LeadDrawer } from './LeadDrawer';

/**
 * Section F — reveal-on-demand. The drawer is the ONLY customer surface that can show
 * a lead's handle, their comment, or the post they wrote it on, and only after an
 * explicit click the bridge audits. The property these tests exist to protect is that
 * the reveal is session-local: if it were cached anywhere, "anonymized by default"
 * would decay into "anonymized until first viewed".
 */

const LEAD = buildMatch({
  commentId: 'c1',
  intent: 'Wants pricing for the Pro plan',
  reason: 'asks price with phone number',
});

const OTHER = buildMatch({ commentId: 'c2', intent: 'Wants a demo next week' });

/**
 * The post pointer lives HERE, on the reveal answer, and no longer on the lead: since
 * v27 an org-facing `Match` carries no `reelId` at all, so there is nothing on the row
 * for a component to build a post URL out of.
 */
const REEL_ID = 'DXOML7vjQhn';

const IDENTITY = {
  username: 'dana_t',
  text: 'How much is the Pro plan? +1 415 555 0142',
  reelId: REEL_ID,
};

/**
 * The page owns which lead the drawer shows, so the test drives that the same way:
 * a host that opens/closes/switches leads through real clicks, rather than re-rendering
 * the drawer in isolation (which would remount it and hide the very state under test).
 */
function DrawerHost() {
  const [lead, setLead] = useState<Match | null>(LEAD);
  return (
    <>
      <button type="button" onClick={() => { setLead(null); }}>close drawer</button>
      <button type="button" onClick={() => { setLead(LEAD); }}>open lead one</button>
      <button type="button" onClick={() => { setLead(OTHER); }}>open lead two</button>
      <LeadDrawer lead={lead} threshold={0.6} onClose={() => { setLead(null); }} />
    </>
  );
}

function renderDrawerAs(role: Role) {
  const repository = new FakePanelRepository(buildPanelState({ MATCHES: [LEAD, OTHER] }));
  repository.currentUser = {
    id: 1,
    email: 'user@aizu.test',
    role,
    orgId: 1,
    org: { id: 1, name: 'Test Co', logo: null, description: null },
  };
  repository.revealIdentities.set(LEAD.id, IDENTITY);
  renderWithProviders(<DrawerHost />, { repository, route: '/leads', path: '/leads' });
  return repository;
}

/** The reel deep link, named by its reel id. Distinct from the `tel:` link that an
 *  extracted phone number renders — that one is contact data the person volunteered. */
function postLink() {
  return screen.queryByRole('link', { name: new RegExp(REEL_ID) });
}

describe('LeadDrawer before a reveal', () => {
  test('shows the intent, the reason and the score — and no identity or post link', async () => {
    renderDrawerAs('owner');

    expect(await screen.findAllByText('Wants pricing for the Pro plan')).not.toHaveLength(0);
    expect(screen.getByText('asks price with phone number')).toBeInTheDocument();
    // Extracted contact fields stay visible: they are what the person volunteered.
    expect(screen.getByText('+14155550142')).toBeInTheDocument();

    expect(screen.queryByText(IDENTITY.username)).not.toBeInTheDocument();
    expect(screen.queryByText(IDENTITY.text)).not.toBeInTheDocument();
    // No deep link to the post — the comment and the handle are visible ON that post,
    // so a link is the redaction undone in one click. Not even the bare reel id shows:
    // it is one hand-built URL away from the same page.
    expect(postLink()).not.toBeInTheDocument();
    expect(document.querySelector('a[href*="instagram.com"]')).toBeNull();
    expect(screen.queryByText(REEL_ID)).not.toBeInTheDocument();
  });

  test('an empty intent renders the neutral placeholder, never an identifier', () => {
    const repository = new FakePanelRepository(buildPanelState());
    renderWithProviders(
      <LeadDrawer lead={buildMatch({ commentId: 'blank', intent: '' })} threshold={0.6} onClose={() => {}} />,
      { repository, route: '/leads', path: '/leads' },
    );
    expect(screen.getByText(LEAD_INTENT_PLACEHOLDER)).toBeInTheDocument();
  });
});

describe('LeadDrawer reveal', () => {
  test('revealing shows the handle, the comment and the post link for that lead', async () => {
    const user = userEvent.setup();
    const repository = renderDrawerAs('owner');

    await user.click(await screen.findByRole('button', { name: /Reveal source/ }));

    expect(await screen.findByText(IDENTITY.username)).toBeInTheDocument();
    expect(screen.getByText(IDENTITY.text)).toBeInTheDocument();
    expect(postLink()).toHaveAttribute('href', `https://www.instagram.com/reel/${REEL_ID}/`);
    // One lead, one call — there is no bulk reveal, and the request identifies exactly
    // the lead on screen so the server can scope it to the caller's org.
    expect(repository.revealRequests).toEqual([
      { campaignId: LEAD.campaignId, platform: LEAD.platform, commentId: LEAD.commentId },
    ]);
  });

  test('a viewer gets no reveal control at all', async () => {
    renderDrawerAs('viewer');

    expect(await screen.findByText(/needs an owner, admin, or member/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Reveal source/ })).not.toBeInTheDocument();
  });

  test('a failed reveal surfaces the error and reveals nothing', async () => {
    const user = userEvent.setup();
    const repository = renderDrawerAs('owner');
    repository.revealFailure = 'network';

    await user.click(await screen.findByRole('button', { name: /Reveal source/ }));

    expect(await screen.findByRole('alert')).toHaveTextContent(/bridge server unreachable/);
    expect(screen.queryByText(IDENTITY.username)).not.toBeInTheDocument();
  });

  /**
   * THE load-bearing test. Revealed identity is component state and nothing else: it is
   * never written into the leads cache, into React Query, or into localStorage, so
   * closing and reopening the drawer re-reveals — and re-audits. A cached reveal would
   * turn one audited click into permanent, unlogged access.
   */
  test('reopening the drawer re-reveals: nothing is persisted and every view is audited', async () => {
    const user = userEvent.setup();
    const repository = renderDrawerAs('owner');

    await user.click(await screen.findByRole('button', { name: /Reveal source/ }));
    expect(await screen.findByText(IDENTITY.username)).toBeInTheDocument();

    // Close the drawer the way the page does — the lead prop goes null.
    await user.click(screen.getByRole('button', { name: 'close drawer' }));
    // Nothing about the reveal survived anywhere a later render could read it back:
    // not browser storage, and not the leads list the page re-reads from the repository.
    expect(JSON.stringify(localStorage)).not.toContain(IDENTITY.username);
    const reloaded = await repository.fetchLeads({ page: 1, pageSize: 50 });
    expect(JSON.stringify(reloaded)).not.toContain(IDENTITY.username);

    // Reopen the SAME lead: it is hidden again, and seeing it costs another audited call.
    await user.click(screen.getByRole('button', { name: 'open lead one' }));
    await waitFor(() => {
      expect(screen.queryByText(IDENTITY.username)).not.toBeInTheDocument();
    });
    expect(repository.revealRequests).toHaveLength(1);

    await user.click(screen.getByRole('button', { name: /Reveal source/ }));
    expect(await screen.findByText(IDENTITY.username)).toBeInTheDocument();
    expect(repository.revealRequests).toHaveLength(2);
  });

  /**
   * The period reveal allowance (402). It is the one reveal failure a customer can act
   * on, so it gets the plan-limit treatment the run gate's 402 gets — plain language
   * plus an upgrade link — and NOT the generic error copy, which reads as a broken
   * button rather than a plan that ran out.
   */
  test('the reveal-allowance 402 renders as a plan limit with an upgrade link', async () => {
    const user = userEvent.setup();
    const repository = renderDrawerAs('owner');
    repository.revealFailure = 'plan_limit';

    await user.click(await screen.findByRole('button', { name: /Reveal source/ }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/revealed every lead your plan includes/i);
    // The server's specifics (cap size, reset date) survive as the detail line.
    expect(alert).toHaveTextContent(/Resets Jul 1/);
    expect(alert).not.toHaveTextContent(/didn’t work/);
    expect(within(alert).getByRole('link', { name: /Upgrade plan/ }))
      .toHaveAttribute('href', '/settings/billing');
    // Refused means refused: no handle, no comment, no post link.
    expect(screen.queryByText(IDENTITY.username)).not.toBeInTheDocument();
    expect(postLink()).not.toBeInTheDocument();
  });

  /**
   * The cap counts DISTINCT leads revealed this period, so a lead already revealed
   * costs nothing to reopen and can never produce the limit. The panel's half of that
   * contract is that a 402 is never sticky: it belongs to one reveal attempt, not to
   * the drawer, the lead, or the session. Cache it and a customer who hit the cap once
   * would be told they were at their limit while reopening leads they already paid for.
   */
  test('a lead that reveals fine shows no plan limit, even after one was hit', async () => {
    const user = userEvent.setup();
    const repository = renderDrawerAs('owner');
    repository.revealFailure = 'plan_limit';

    await user.click(await screen.findByRole('button', { name: /Reveal source/ }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/revealed every lead your plan/i);

    // Reopen the same lead — an already-revealed lead is exactly this case on the wire.
    await user.click(screen.getByRole('button', { name: 'close drawer' }));
    await user.click(screen.getByRole('button', { name: 'open lead one' }));
    // The refusal did not survive the reopen: the drawer is back to its hidden state.
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /Reveal source/ }));
    expect(await screen.findByText(IDENTITY.username)).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /Upgrade plan/ })).not.toBeInTheDocument();
  });

  test('moving to another lead drops the previous lead’s revealed identity', async () => {
    const user = userEvent.setup();
    renderDrawerAs('owner');

    await user.click(await screen.findByRole('button', { name: /Reveal source/ }));
    expect(await screen.findByText(IDENTITY.username)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'open lead two' }));

    await waitFor(() => {
      expect(screen.queryByText(IDENTITY.username)).not.toBeInTheDocument();
    });
    expect(screen.getByRole('button', { name: /Reveal source/ })).toBeInTheDocument();
  });
});
