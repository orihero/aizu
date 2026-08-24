import { describe, expect, test } from 'vitest';
import { fleetResponseSchema, preflightSummarySchema } from './admin';

/**
 * Boundary tests for the superadmin fleet payload, aimed squarely at the B4 trap.
 *
 * B4 is "the field is carried correctly by every layer and then silently dropped at the
 * last hop". It has shipped inert twice. On the panel side the mechanism is that
 * `z.object()` STRIPS unknown keys — so a `preflight` the worker measured, the server
 * validated and the store persisted vanishes on parse, and the fleet console renders a
 * parked box as perfectly healthy. Nothing fails; the data is just gone.
 *
 * These tests parse a payload shaped exactly like the served `/api/admin/fleet` response,
 * so a schema that forgets the key fails here rather than in production.
 */

/** One worker exactly as `server._handle_admin_fleet` serialises it. */
function serverWorker(extra: Record<string, unknown> = {}) {
  return {
    id: 'w_7f3c1e',
    orgId: 4,
    displayName: 'OPS-PC-3',
    host: 'ops-pc-3',
    os: 'Windows 11',
    agentVersion: '1.4.2',
    maxSessions: 1,
    currentSessions: 0,
    capabilities: [],
    registeredAt: 1_786_800_000,
    lastHeartbeatAt: 1_786_800_004,
    lastSeenAgeSec: 4.2,
    status: 'online',
    revokedAt: null,
    currentJob: null,
    ...extra,
  };
}

function parseFleet(workers: unknown[]) {
  const result = fleetResponseSchema.safeParse({
    ok: true, data: { workers }, error: null,
  });
  expect(result.success).toBe(true);
  return result.success ? result.data.data!.workers : [];
}

describe('fleetWorkerSchema · the preflight field (B4)', () => {
  test('a blocking preflight survives the parse instead of being stripped', () => {
    const [worker] = parseFleet([
      serverWorker({
        preflight: {
          ok: false, blocking: true, enforced: true, ranAt: 1_786_800_000.12,
          failed: [
            {
              id: 'token_persistence', severity: 'fatal', status: 'fail',
              detail: 'encrypted-file backend: SecretCipherError: AIZU_SECRET_KEY is not set',
            },
            { id: 'capabilities', severity: 'fatal', status: 'fail', detail: 'neither var is set' },
          ],
        },
      }),
    ]);

    expect(worker!.preflight).not.toBeNull();
    expect(worker!.preflight!.blocking).toBe(true);
    expect(worker!.preflight!.failed.map((r) => r.id))
      .toEqual(['token_persistence', 'capabilities']);
    expect(worker!.preflight!.failed[0]!.detail).toContain('AIZU_SECRET_KEY');
  });

  test('status rides through, so "could not check" stays distinct from "broken"', () => {
    const [worker] = parseFleet([
      serverWorker({
        preflight: {
          ok: false, blocking: true, enforced: true, ranAt: 1,
          failed: [
            { id: 'cdp_reachable', severity: 'fatal', status: 'fail', detail: 'nothing answers' },
            { id: 'login.instagram', severity: 'warn', status: 'unknown', detail: 'skipped' },
          ],
        },
      }),
    ]);

    expect(worker!.preflight!.failed.map((r) => r.status)).toEqual(['fail', 'unknown']);
  });

  test('a row from an older sidecar with no status degrades to fail, never to pass', () => {
    const [worker] = parseFleet([
      serverWorker({
        preflight: {
          ok: false, blocking: true, enforced: true, ranAt: 1,
          failed: [{ id: 'capabilities', severity: 'fatal', detail: 'x' }],
        },
      }),
    ]);

    expect(worker!.preflight!.failed[0]!.status).toBe('fail');
  });

  test('one row with an unknown severity cannot drop the whole failed list', () => {
    // The B4 trap one field over: `failed: z.array(...).catch([])` means a single
    // unparseable ROW costs the console EVERY row. A severity this panel has not
    // learned yet (a newer sidecar's) must degrade to fatal, not delete its siblings.
    const [worker] = parseFleet([
      serverWorker({
        preflight: {
          ok: false, blocking: true, enforced: true, ranAt: 1,
          failed: [
            { id: 'llm_credential', severity: 'notice', status: 'fail', detail: 'no key' },
            { id: 'capabilities', severity: 'fatal', status: 'fail', detail: 'neither var is set' },
          ],
        },
      }),
    ]);

    expect(worker!.preflight!.failed.map((r) => r.id))
      .toEqual(['llm_credential', 'capabilities']);
    expect(worker!.preflight!.failed[0]!.severity).toBe('fatal');
  });

  test('an older server that omits preflight entirely still parses, as null', () => {
    const [worker] = parseFleet([serverWorker()]);
    expect(worker!.preflight).toBeNull();
  });

  test('a garbage preflight degrades to null rather than failing the whole fleet', () => {
    // A diagnostic must never be the reason the console cannot render the fleet.
    const [worker] = parseFleet([serverWorker({ preflight: 'not-an-object' })]);
    expect(worker!.preflight).toBeNull();
  });
});

describe('preflightSummarySchema', () => {
  test('a malformed failed[] collapses to an empty list, keeping the verdict', () => {
    const parsed = preflightSummarySchema.parse({
      ok: false, blocking: true, enforced: true, ranAt: null, failed: 'nope',
    });
    expect(parsed).toEqual({
      ok: false, blocking: true, enforced: true, ranAt: null, failed: [],
    });
  });
});
