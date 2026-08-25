import { describe, expect, test } from 'vitest';
import { render, screen } from '@testing-library/react';
import { DndContext } from '@dnd-kit/core';
import { buildMatch, buildPanelState } from '@/test/fixtures';
import { FakePanelRepository } from '@/test/fakePanelRepository';
import { renderWithProviders } from '@/test/renderWithProviders';
import { matchSchema, tickerEntrySchema } from '@/shared/schemas/panelState';
import type { Match, TickerEntry } from '@/shared/types/domain';
import { LEAD_INTENT_PLACEHOLDER } from '@/shared/selectors/leads';
import { LiveTickerTile } from '@/features/dashboard/LiveTickerTile';
import { LeadsTable } from './LeadsTable';
import { LeadCard } from './board/LeadCard';
import { LeadsPage } from './LeadsPage';

/**
 * v27 redaction sweep. Change #2 in the shared contract: an org-facing lead carries a
 * one-line INTENT and no identity at all. Raw handle and comment live on in the DB and
 * are reachable two ways only — the superadmin plane, and the drawer's audited
 * per-lead reveal (Section F).
 *
 * The per-surface suites each check their own rendering; this file is the cross-surface
 * net. It asserts the property that actually matters — "an identity string a server
 * could still be sending never reaches the DOM" — on every list surface at once, so a
 * new leads surface that reintroduces a handle fails here even if its own suite is
 * written to match the mistake.
 */

/** The identity a pre-v27 bridge (or a bug) might still put on the wire. */
const HANDLE = 'dana_t';
const COMMENT = 'How much is the Pro plan? +1 415 555 0142';

const INTENT = 'Wants pricing for the Pro plan';

/** A dashboard ticker row. Not a `Match` — it is its own, thinner shape. */
function tickerRow(intent: string): TickerEntry {
  return {
    id: 'inst:c1',
    intent,
    platform: 'instagram',
    score: 0.91,
    capturedAt: { date: 'Jun 19', time: '14:20' },
  };
}

/** A lead as the Zod boundary would hand it to the app: the extra keys are stripped. */
function parsedLead(overrides: Partial<Match> = {}): Match {
  return {
    ...matchSchema.parse({ ...buildMatch({ intent: INTENT }), username: HANDLE, text: COMMENT }),
    ...overrides,
  };
}

/**
 * A lead that STILL CARRIES identity, smuggled past the type. This is the fixture the
 * render tests below use, and using it is the whole point: a row built through
 * `matchSchema` has nothing to leak, so asserting on one would only re-test the schema
 * and would stay green if a component started printing `lead.username` again. These
 * rows make the component itself the thing under test.
 */
function leakyLead(overrides: Partial<Match> = {}): Match {
  return {
    ...buildMatch({ intent: INTENT }),
    username: HANDLE,
    text: COMMENT,
    ...overrides,
  } as unknown as Match;
}

describe('the lead shape itself carries no identity', () => {
  test('a Match parsed from a leaky payload has neither key', () => {
    const lead = parsedLead();
    expect(lead).not.toHaveProperty('username');
    expect(lead).not.toHaveProperty('text');
    expect(JSON.stringify(lead)).not.toContain(HANDLE);
    expect(JSON.stringify(lead)).not.toContain(COMMENT);
  });

  test('a ticker entry parsed from a leaky payload has no handle', () => {
    const entry = tickerEntrySchema.parse({ ...tickerRow(INTENT), username: HANDLE });
    expect(entry).not.toHaveProperty('username');
    expect(entry.intent).toBeTypeOf('string');
  });

  /**
   * Compile-time guard. `HasKey<Match, 'username'>` resolves to `true` the moment the
   * key comes back onto the domain type, and `true` is not assignable to the `false`
   * annotation — so re-adding `username`/`text` to `Match` fails `npm run typecheck`,
   * not just this assertion. The runtime `expect` keeps the guard from being deleted as
   * an unused binding.
   */
  test('the domain type has no username/text key (checked by tsc, not just at runtime)', () => {
    type HasKey<T, K extends string> = K extends keyof T ? true : false;
    const usernameOnLead: HasKey<Match, 'username'> = false;
    const textOnLead: HasKey<Match, 'text'> = false;
    const usernameOnTicker: HasKey<TickerEntry, 'username'> = false;
    expect([usernameOnLead, textOnLead, usernameOnTicker]).toEqual([false, false, false]);
  });
});

describe('no customer-facing list surface renders a handle or a comment', () => {
  const noop = () => {};

  test('the leads table shows the intent and nothing that identifies the person', () => {
    render(
      <LeadsTable
        rows={[leakyLead()]}
        threshold={0.7}
        selected={new Set()}
        sort={{ key: 'captured', dir: 'desc' }}
        onSort={noop}
        onToggleSelect={noop}
        onToggleSelectAll={noop}
        onOpen={noop}
      />,
    );

    expect(screen.getByText(INTENT)).toBeInTheDocument();
    expect(document.body.textContent).not.toContain(HANDLE);
    expect(document.body.textContent).not.toContain(COMMENT);
    // Not even as a tooltip, an aria-label, or an image alt.
    expect(document.body.innerHTML).not.toContain(HANDLE);
  });

  test('the board card shows the intent and nothing that identifies the person', () => {
    render(
      <DndContext>
        <LeadCard lead={leakyLead()} threshold={0.7} onOpen={noop} draggable />
      </DndContext>,
    );

    expect(screen.getByText(INTENT)).toBeInTheDocument();
    expect(document.body.innerHTML).not.toContain(HANDLE);
    expect(document.body.innerHTML).not.toContain(COMMENT);
  });

  test('the dashboard ticker shows the intent and nothing that identifies the person', () => {
    render(<LiveTickerTile entries={[tickerRow(INTENT)]} />);

    expect(screen.getByText(INTENT)).toBeInTheDocument();
    expect(document.body.innerHTML).not.toContain(HANDLE);
  });

  test('an empty intent renders the placeholder, never a fallback identifier', () => {
    // The failure this catches is the tempting one: "no intent yet, show the handle so
    // the row is not blank". A blank-looking row is the correct outcome.
    render(<LiveTickerTile entries={[tickerRow('  ')]} />);
    expect(screen.getByText(LEAD_INTENT_PLACEHOLDER)).toBeInTheDocument();
    expect(document.body.innerHTML).not.toContain(HANDLE);
  });

  test('the whole leads page renders no identity for a leaky server payload', async () => {
    const repo = new FakePanelRepository(buildPanelState({ MATCHES: [leakyLead()] }));
    renderWithProviders(<LeadsPage />, { repository: repo, route: '/leads', path: '/leads' });

    expect(await screen.findByText(INTENT)).toBeInTheDocument();
    expect(document.body.innerHTML).not.toContain(HANDLE);
    expect(document.body.innerHTML).not.toContain(COMMENT);
  });
});

describe('the ticker entry is not a Match — it shares the copy, not the helper', () => {
  test('placeholder copy is identical across the table and the ticker', () => {
    render(<LiveTickerTile entries={[tickerRow('')]} />);
    const tickerCopy = screen.getByText(LEAD_INTENT_PLACEHOLDER).textContent;

    render(
      <LeadsTable
        rows={[leakyLead({ intent: '' })]}
        threshold={0.7}
        selected={new Set()}
        sort={{ key: 'captured', dir: 'desc' }}
        onSort={() => {}}
        onToggleSelect={() => {}}
        onToggleSelectAll={() => {}}
        onOpen={() => {}}
      />,
    );

    expect(screen.getAllByText(LEAD_INTENT_PLACEHOLDER).length).toBeGreaterThanOrEqual(2);
    expect(tickerCopy).toBe(LEAD_INTENT_PLACEHOLDER);
  });
});
