import { afterEach, describe, expect, test, vi } from 'vitest';
import { buildPanelState } from '@/test/fixtures';
import { HttpPanelRepository } from './httpPanelRepository';

function mockFetchOnce(response: Partial<Response> & { jsonValue?: unknown }) {
  const { jsonValue, ...rest } = response;
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve(jsonValue),
      ...rest,
    }),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('HttpPanelRepository.fetchState', () => {
  test('returns validated state on success', async () => {
    // Arrange
    mockFetchOnce({ jsonValue: buildPanelState() });
    const repository = new HttpPanelRepository();

    // Act
    const result = await repository.fetchState();

    // Assert
    expect(result.ok).toBe(true);
    if (result.ok) expect(result.value.CONFIG.productName).toBe('AIZU');
  });

  test('returns a network error instead of throwing when fetch rejects', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('ECONNREFUSED')));
    const result = await new HttpPanelRepository().fetchState();
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error.kind).toBe('network');
  });

  test('returns an http error for non-2xx responses', async () => {
    mockFetchOnce({ ok: false, status: 500, jsonValue: {} });
    const result = await new HttpPanelRepository().fetchState();
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toMatchObject({ kind: 'http', status: 500 });
  });

  test('returns a validation error for shape mismatches', async () => {
    mockFetchOnce({ jsonValue: { CONFIG: { productName: 42 } } });
    const result = await new HttpPanelRepository().fetchState();
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error.kind).toBe('validation');
  });
});

describe('HttpPanelRepository.setMatchStatus', () => {
  const REQUEST = { campaignId: 'cmp-001', commentId: 'c1', status: 'interested' } as const;

  test('POSTs the request body and succeeds on an ok envelope', async () => {
    // Arrange
    mockFetchOnce({
      jsonValue: { ok: true, data: { commentId: 'c1', status: 'interested' }, error: null },
    });
    const repository = new HttpPanelRepository();

    // Act
    const result = await repository.setMatchStatus(REQUEST);

    // Assert
    expect(result.ok).toBe(true);
    const fetchMock = vi.mocked(fetch);
    const [url, init] = fetchMock.mock.calls[0] ?? [];
    expect(url).toBe('/api/status');
    expect(init?.method).toBe('POST');
    expect(JSON.parse(init?.body as string)).toEqual(REQUEST);
  });

  test('surfaces the server error message on a rejected envelope', async () => {
    mockFetchOnce({
      ok: false,
      status: 404,
      jsonValue: { ok: false, data: null, error: "no match for comment_id 'c1'" },
    });
    const result = await new HttpPanelRepository().setMatchStatus(REQUEST);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error.message).toContain('no match');
  });

  test('returns an http error when the body is not JSON', async () => {
    mockFetchOnce({ status: 502, json: () => Promise.reject(new Error('not json')) });
    const result = await new HttpPanelRepository().setMatchStatus(REQUEST);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error.kind).toBe('http');
  });
});

describe('HttpPanelRepository.runCampaign', () => {
  const RUN_REQUEST = { campaignId: 'cmp-001', mode: 'dry' } as const;

  test('POSTs to /api/run and succeeds on a 202 accept envelope', async () => {
    // Arrange
    mockFetchOnce({
      status: 202,
      jsonValue: {
        ok: true,
        data: { accepted: true, scope: 'campaign', campaignId: 'cmp-001', mode: 'dry' },
        error: null,
      },
    });
    const repository = new HttpPanelRepository();

    // Act
    const result = await repository.runCampaign(RUN_REQUEST);

    // Assert
    expect(result.ok).toBe(true);
    const fetchMock = vi.mocked(fetch);
    const [url, init] = fetchMock.mock.calls[0] ?? [];
    expect(url).toBe('/api/run');
    expect(init?.method).toBe('POST');
    expect(JSON.parse(init?.body as string)).toEqual(RUN_REQUEST);
  });

  test('surfaces the 409 "already active" message with its status', async () => {
    mockFetchOnce({
      ok: false,
      status: 409,
      jsonValue: { ok: false, data: null, error: 'a run is already active' },
    });
    const result = await new HttpPanelRepository().runCampaign(RUN_REQUEST);
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error.status).toBe(409);
      expect(result.error.message).toContain('already active');
    }
  });

  test('surfaces a 400 not-runnable error with its status', async () => {
    mockFetchOnce({
      ok: false,
      status: 400,
      jsonValue: { ok: false, data: null, error: 'campaign is not runnable' },
    });
    const result = await new HttpPanelRepository().runCampaign({ campaignId: 'draft-x', mode: 'dry' });
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error.status).toBe(400);
  });

  test('returns a network error instead of throwing when fetch rejects', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('ECONNREFUSED')));
    const result = await new HttpPanelRepository().runCampaign(RUN_REQUEST);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error.kind).toBe('network');
  });

  test('surfaces a 409 agent-not-ready gate (its own shape, not the write envelope) tagged with code', async () => {
    mockFetchOnce({
      ok: false,
      status: 409,
      jsonValue: {
        error: 'agent_not_ready',
        detail: 'Chrome (CDP) unreachable — launch the login browser first.',
        readiness: {
          ready: false, cdp: 'unreachable', instagram: 'unknown',
          checkedAt: 1_718_800_000, detail: 'connect ECONNREFUSED 127.0.0.1:9222',
          cdpUrl: 'http://127.0.0.1:9222',
        },
      },
    });
    const result = await new HttpPanelRepository().runCampaign(RUN_REQUEST);
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error.status).toBe(409);
      expect(result.error.code).toBe('agent_not_ready');
      expect(result.error.message).toContain('Chrome (CDP) unreachable');
    }
  });
});

describe('HttpPanelRepository.getAgentReadiness', () => {
  const READINESS = {
    ready: true, cdp: 'ok', instagram: 'logged_in',
    checkedAt: 1_718_800_000, detail: null, cdpUrl: 'http://127.0.0.1:9222',
  };

  test('GETs the raw readiness dict (no {ok,data,error} envelope)', async () => {
    mockFetchOnce({ jsonValue: READINESS });
    const result = await new HttpPanelRepository().getAgentReadiness();
    expect(result.ok).toBe(true);
    if (result.ok) expect(result.value).toEqual(READINESS);
    expect(vi.mocked(fetch).mock.calls[0]?.[0]).toBe('/api/agent/readiness');
  });

  test('appends refresh=1 when a live probe is requested', async () => {
    mockFetchOnce({ jsonValue: READINESS });
    await new HttpPanelRepository().getAgentReadiness({ refresh: true });
    expect(vi.mocked(fetch).mock.calls[0]?.[0]).toBe('/api/agent/readiness?refresh=1');
  });

  // The server grew ?campaign=<id> to narrow the fleet answer to the platforms that
  // campaign needs, but for one round NO client sent it — so the narrowing was live on
  // the endpoint and inert in the product (the same "added to the payload, never sent
  // on the wire" trap that has shipped here before). These tests pin the query string
  // the server actually parses; they fail if the parameter stops being sent.
  test('sends campaign=<id> when the caller scopes the question to one campaign', async () => {
    mockFetchOnce({ jsonValue: READINESS });
    await new HttpPanelRepository().getAgentReadiness({ campaignId: 'o1.q4-outbound' });
    expect(vi.mocked(fetch).mock.calls[0]?.[0]).toBe(
      '/api/agent/readiness?campaign=o1.q4-outbound');
  });

  test('sends both refresh and campaign together', async () => {
    mockFetchOnce({ jsonValue: READINESS });
    await new HttpPanelRepository().getAgentReadiness({
      refresh: true, campaignId: 'o1.q4-outbound' });
    expect(vi.mocked(fetch).mock.calls[0]?.[0]).toBe(
      '/api/agent/readiness?refresh=1&campaign=o1.q4-outbound');
  });

  test('percent-encodes a campaign id rather than interpolating it', async () => {
    mockFetchOnce({ jsonValue: READINESS });
    await new HttpPanelRepository().getAgentReadiness({ campaignId: 'o1.a b&c=d' });
    expect(vi.mocked(fetch).mock.calls[0]?.[0]).toBe(
      '/api/agent/readiness?campaign=o1.a+b%26c%3Dd');
  });

  test('returns an http error for non-2xx responses', async () => {
    mockFetchOnce({ ok: false, status: 500, jsonValue: {} });
    const result = await new HttpPanelRepository().getAgentReadiness();
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toMatchObject({ kind: 'http', status: 500 });
  });
});

describe('HttpPanelRepository.launchAgentLogin', () => {
  test('POSTs and returns launched + the re-checked readiness on success', async () => {
    mockFetchOnce({
      jsonValue: {
        launched: true,
        readiness: {
          ready: false, cdp: 'ok', instagram: 'logged_out',
          checkedAt: 1_718_800_000, detail: 'awaiting login', cdpUrl: 'http://127.0.0.1:9222',
        },
      },
    });
    const result = await new HttpPanelRepository().launchAgentLogin();
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.value.launched).toBe(true);
      expect(result.value.readiness.instagram).toBe('logged_out');
    }
    const [url, init] = vi.mocked(fetch).mock.calls[0] ?? [];
    expect(url).toBe('/api/agent/launch-login');
    expect(init?.method).toBe('POST');
  });

  test('surfaces the 500 launch_failed detail message', async () => {
    mockFetchOnce({
      ok: false,
      status: 500,
      jsonValue: { error: 'launch_failed', detail: 'Chrome executable not found' },
    });
    const result = await new HttpPanelRepository().launchAgentLogin();
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error.status).toBe(500);
      expect(result.error.message).toBe('Chrome executable not found');
    }
  });

  test('falls back to a generic message for a 403 (not owner/admin) with no failure body', async () => {
    mockFetchOnce({ ok: false, status: 403, jsonValue: { ok: false, error: 'forbidden' } });
    const result = await new HttpPanelRepository().launchAgentLogin();
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error.status).toBe(403);
  });

  test('returns a network error instead of throwing when fetch rejects', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('ECONNREFUSED')));
    const result = await new HttpPanelRepository().launchAgentLogin();
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error.kind).toBe('network');
  });
});

describe('HttpPanelRepository.fetchRunActivity', () => {
  const ACTIVITY = {
    runId: 'abc123',
    finished: false,
    counters: {
      reelsSeen: 12, relevancePasses: 5, commentsScored: 40,
      matches: 3, spendUsd: 0.0123, likes: 2, follows: 1,
    },
    events: [
      {
        id: 142, seq: 3, campaignId: 'c1', phase: 'comments', level: 'success',
        message: 'Match: @aziz (score 0.82)', detail: '{"username":"aziz","score":0.82,"tier":"A"}',
        createdAt: 1_718_800_000.123,
      },
    ],
    flags: [],
    // cursor is the max event id in the page (global), not seq.
    cursor: 142,
  };

  test('GETs the runId + after query params and unwraps the data on 200', async () => {
    // Arrange
    mockFetchOnce({ jsonValue: { ok: true, data: ACTIVITY, error: null } });
    const repository = new HttpPanelRepository();

    // Act
    const result = await repository.fetchRunActivity('abc123', 1);

    // Assert
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.value.cursor).toBe(142); // max event id (global), not seq
      expect(result.value.events[0]?.id).toBe(142);
    }
    const [url, init] = vi.mocked(fetch).mock.calls[0] ?? [];
    expect(url).toBe('/api/run/activity?runId=abc123&after=1');
    expect(init?.credentials).toBe('same-origin');
  });

  test('defaults the after cursor to 0 when omitted', async () => {
    mockFetchOnce({ jsonValue: { ok: true, data: ACTIVITY, error: null } });
    await new HttpPanelRepository().fetchRunActivity('abc123');
    expect(vi.mocked(fetch).mock.calls[0]?.[0]).toBe('/api/run/activity?runId=abc123&after=0');
  });

  test('surfaces a 404 unknown-run error with its status', async () => {
    mockFetchOnce({
      ok: false,
      status: 404,
      jsonValue: { ok: false, data: null, error: 'unknown run' },
    });
    const result = await new HttpPanelRepository().fetchRunActivity('missing');
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error.status).toBe(404);
      expect(result.error.message).toContain('unknown run');
    }
  });

  test('returns a network error instead of throwing when fetch rejects', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('ECONNREFUSED')));
    const result = await new HttpPanelRepository().fetchRunActivity('abc123');
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error.kind).toBe('network');
  });

  test('parses a fleetJob block for a fleet-routed run (FIX 2)', async () => {
    mockFetchOnce({
      jsonValue: {
        ok: true,
        data: {
          ...ACTIVITY,
          fleetJob: { jobId: 'job-9', status: 'running', lastEventAt: 1_718_800_000, leaseExpiresAt: 1_718_800_060 },
        },
        error: null,
      },
    });
    const result = await new HttpPanelRepository().fetchRunActivity('abc123', 1);
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.value.fleetJob?.jobId).toBe('job-9');
      expect(result.value.fleetJob?.status).toBe('running');
    }
  });

  test('defaults fleetJob to null for an in-process run (key absent)', async () => {
    mockFetchOnce({ jsonValue: { ok: true, data: ACTIVITY, error: null } });
    const result = await new HttpPanelRepository().fetchRunActivity('abc123', 1);
    expect(result.ok).toBe(true);
    if (result.ok) expect(result.value.fleetJob).toBeNull();
  });
});

describe('HttpPanelRepository.generateCampaign', () => {
  const INPUT = { text: 'a team project-management SaaS' } as const;

  test('POSTs the input and returns the validated draft on success', async () => {
    mockFetchOnce({
      jsonValue: {
        ok: true,
        data: { name: 'Acme Analytics', objective: 'lead', platform: 'instagram', languages: 'en' },
        error: null,
      },
    });

    const result = await new HttpPanelRepository().generateCampaign(INPUT);

    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.value.name).toBe('Acme Analytics');
      expect(result.value.languages).toBe('en');
    }
    const [url, init] = vi.mocked(fetch).mock.calls[0] ?? [];
    expect(url).toBe('/api/campaign/generate');
    expect(init?.method).toBe('POST');
    expect(JSON.parse(init?.body as string)).toEqual(INPUT);
  });

  test('degrades a ragged draft to a partial prefill rather than failing', async () => {
    // The model returned a number where a string was expected — the tolerant
    // schema coerces it, so the user still gets a usable prefill.
    mockFetchOnce({ jsonValue: { ok: true, data: { name: 'Ok', relevanceDef: 42 }, error: null } });
    const result = await new HttpPanelRepository().generateCampaign(INPUT);
    expect(result.ok).toBe(true);
    if (result.ok) expect(result.value.relevanceDef).toBe('');
  });

  test('surfaces a 503 no-AI-key error with its status', async () => {
    mockFetchOnce({
      ok: false,
      status: 503,
      jsonValue: { ok: false, data: null, error: 'OPENROUTER_API_KEY is not configured' },
    });
    const result = await new HttpPanelRepository().generateCampaign(INPUT);
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error.status).toBe(503);
      expect(result.error.message).toMatch(/OPENROUTER/);
    }
  });

  test('surfaces a 422 unbuildable-input error with its status', async () => {
    mockFetchOnce({
      ok: false,
      status: 422,
      jsonValue: { ok: false, data: null, error: 'could not draft a campaign from that input' },
    });
    const result = await new HttpPanelRepository().generateCampaign(INPUT);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error.status).toBe(422);
  });

  test('returns a network error instead of throwing when fetch rejects', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('ECONNREFUSED')));
    const result = await new HttpPanelRepository().generateCampaign(INPUT);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error.kind).toBe('network');
  });
});

describe('HttpPanelRepository.runInterview', () => {
  const INPUT = { text: 'a team project-management SaaS', round: 1 } as const;

  test('POSTs the input and returns the validated round on success', async () => {
    mockFetchOnce({
      jsonValue: {
        ok: true,
        data: {
          done: false,
          round: 1,
          productContext: 'PRODUCT DESCRIPTION:\nsaas',
          questions: [{ id: 'platforms', type: 'platforms', prompt: 'Where?', suggested: ['instagram'] }],
        },
        error: null,
      },
    });

    const result = await new HttpPanelRepository().runInterview(INPUT);

    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.value.done).toBe(false);
      expect(result.value.questions[0]?.type).toBe('platforms');
      expect(result.value.productContext).toContain('saas');
    }
    const [url, init] = vi.mocked(fetch).mock.calls[0] ?? [];
    expect(url).toBe('/api/campaign/interview');
    expect(init?.method).toBe('POST');
    expect(JSON.parse(init?.body as string)).toEqual(INPUT);
  });

  test('degrades a ragged question to a usable shape rather than failing', async () => {
    // An unknown question type and a non-string prompt are coerced (type→text,
    // prompt→''); the round still validates so the wizard can proceed.
    mockFetchOnce({
      jsonValue: {
        ok: true,
        data: { done: false, round: 1, productContext: 'ctx', questions: [{ id: 'a', type: 'weird', prompt: 7 }] },
        error: null,
      },
    });
    const result = await new HttpPanelRepository().runInterview(INPUT);
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.value.questions[0]?.type).toBe('text');
      expect(result.value.questions[0]?.prompt).toBe('');
    }
  });

  test('surfaces a 503 no-AI-key error with its status', async () => {
    mockFetchOnce({
      ok: false,
      status: 503,
      jsonValue: { ok: false, data: null, error: 'OPENROUTER_API_KEY is not configured' },
    });
    const result = await new HttpPanelRepository().runInterview(INPUT);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error.status).toBe(503);
  });

  test('returns a network error instead of throwing when fetch rejects', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('ECONNREFUSED')));
    const result = await new HttpPanelRepository().runInterview(INPUT);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error.kind).toBe('network');
  });
});

describe('HttpPanelRepository auth', () => {
  const CREDS = { email: 'a@b.com', password: 'longenough1' } as const;

  test('login POSTs credentials with the session cookie and returns the user', async () => {
    const user = {
      id: 1, email: 'a@b.com', role: 'owner', orgId: 1,
      org: { id: 1, name: 'Acme', logo: null, description: null },
    };
    mockFetchOnce({ jsonValue: { ok: true, data: { user }, error: null } });

    const result = await new HttpPanelRepository().login(CREDS);

    expect(result.ok).toBe(true);
    if (result.ok) expect(result.value).toEqual(user);
    const [url, init] = vi.mocked(fetch).mock.calls[0] ?? [];
    expect(url).toBe('/api/auth/login');
    expect(init?.method).toBe('POST');
    expect(init?.credentials).toBe('same-origin'); // sends the HttpOnly cookie
    expect(JSON.parse(init?.body as string)).toEqual(CREDS);
  });

  test('login surfaces the 401 server message without throwing', async () => {
    mockFetchOnce({
      ok: false,
      status: 401,
      jsonValue: { ok: false, data: null, error: 'invalid email or password' },
    });
    const result = await new HttpPanelRepository().login(CREDS);
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error.status).toBe(401);
      expect(result.error.message).toContain('invalid email or password');
    }
  });

  test('signup POSTs to /api/auth/signup and returns the created user', async () => {
    mockFetchOnce({ jsonValue: { ok: true, data: { user: { id: 2, email: 'a@b.com' } }, error: null } });
    const result = await new HttpPanelRepository().signup(CREDS);
    expect(result.ok).toBe(true);
    if (result.ok) expect(result.value.id).toBe(2);
    const [url, init] = vi.mocked(fetch).mock.calls[0] ?? [];
    expect(url).toBe('/api/auth/signup');
    expect(init?.credentials).toBe('same-origin'); // sets the session cookie
  });

  test('signup surfaces a 409 duplicate-email error', async () => {
    mockFetchOnce({
      ok: false,
      status: 409,
      jsonValue: { ok: false, data: null, error: 'an account with that email already exists' },
    });
    const result = await new HttpPanelRepository().signup(CREDS);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error.status).toBe(409);
  });

  test('getCurrentUser returns the user on a 200 envelope', async () => {
    mockFetchOnce({ jsonValue: { ok: true, data: { user: { id: 1, email: 'a@b.com' } }, error: null } });
    const result = await new HttpPanelRepository().getCurrentUser();
    expect(result.ok).toBe(true);
    if (result.ok) expect(result.value?.email).toBe('a@b.com');
  });

  test('getCurrentUser maps a 401 to ok(null), not an error', async () => {
    mockFetchOnce({ ok: false, status: 401, jsonValue: { ok: false, data: null, error: 'not authenticated' } });
    const result = await new HttpPanelRepository().getCurrentUser();
    expect(result.ok).toBe(true);
    if (result.ok) expect(result.value).toBeNull();
  });

  test('getCurrentUser returns a network error instead of throwing when fetch rejects', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('ECONNREFUSED')));
    const result = await new HttpPanelRepository().getCurrentUser();
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error.kind).toBe('network');
  });

  test('logout POSTs to /api/auth/logout and succeeds on an ok envelope', async () => {
    mockFetchOnce({ jsonValue: { ok: true, data: { loggedOut: true }, error: null } });
    const result = await new HttpPanelRepository().logout();
    expect(result.ok).toBe(true);
    expect(vi.mocked(fetch).mock.calls[0]?.[0]).toBe('/api/auth/logout');
  });
});
