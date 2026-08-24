import { useEffect, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { Loader2, Pause, Play, Zap } from 'lucide-react';
import { Button } from '@/shared/ui/Button';
import { Drawer } from '@/shared/ui/Drawer';
import { cn } from '@/shared/lib/cn';
import { queryKeys } from '@/shared/api/queryKeys';
import { ResultError, type AppError } from '@/shared/lib/result';
import { usePauseRun, useResumeRun, useRunCampaign, useStopRun } from '@/shared/hooks/useWriteMutations';
import { useRunActivity } from '@/shared/hooks/useRunActivity';
import { useAgentReadiness } from '@/shared/hooks/useAgentReadiness';
import { useCan } from '@/shared/hooks/useCan';
import {
  selectIsAnyRunActive,
  selectIsCampaignRunning,
  selectIsRunPaused,
  selectLastRunForCampaign,
} from '@/shared/selectors/campaigns';
import type { Campaign, RunBlock } from '@/shared/types/domain';
import { describeRunStartError } from './describeRunStartError';
import { RunActivityFeed } from './RunActivityFeed';

// Quick-pick lead targets; custom covers anything in [1, MAX_LEADS].
const LEAD_PRESETS = [10, 25, 50, 100] as const;
const DEFAULT_LEADS = 25;
const MAX_LEADS = 1000; // mirrors the server's MAX_RUN_LEAD_TARGET
// Wall-clock safety cap (minutes): stop even if the lead target isn't reached.
const MIN_MINUTES = 5;
const MAX_MINUTES = 720;
const DEFAULT_CAP_MINUTES = 120;

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : 'Something went wrong.';
}

/** react-query's mutation `.error` is `unknown`, but every write here goes through
 * `unwrap()`, which only ever throws a ResultError — recover the typed AppError so
 * describeRunStartError can branch on its `code`, not just its message text. */
function toAppError(error: unknown): AppError {
  return error instanceof ResultError
    ? error.appError
    : { kind: 'unknown', message: errorMessage(error) };
}

interface RunStartErrorProps {
  readonly error: unknown;
}

/** The run-start (POST /api/run) inline error. A plain conflict/validation failure
 * just shows the server's message; the 409 agent-not-ready gate additionally points
 * the operator at the fix — the global banner for an admin, "ask an admin" otherwise. */
function RunStartError({ error }: RunStartErrorProps) {
  const canFixAgent = useCan('fix_agent');
  const described = describeRunStartError(toAppError(error));
  return (
    <div className="space-y-1">
      <p className="text-xs font-medium text-danger">{described.message}</p>
      {described.agentNotReady ? (
        <p className="text-xs font-medium text-text-faint">
          {canFixAgent
            ? 'Use the "Instagram agent" banner at the top of the app to fix this, then try again.'
            : 'An administrator needs to fix this (see the banner at the top of the app) before a run can start.'}
        </p>
      ) : null}
    </div>
  );
}

interface RunDrawerProps {
  readonly campaign: Campaign;
  readonly run: RunBlock;
  readonly isOpen: boolean;
  readonly onClose: () => void;
}

/**
 * Right-side drawer to launch (and stop) a live run for one campaign. The operator
 * picks how many leads to collect — a preset or a custom count — plus a max-time
 * safety cap, and Start fires a single live run; the engine loops discovery sessions
 * until it hits the target (or the cap / 9pm window), then stops on its own. While
 * this campaign's run is in flight the drawer shows a Stop control instead.
 */
export function RunDrawer({ campaign, run, isOpen, onClose }: RunDrawerProps) {
  const runCampaign = useRunCampaign();
  const stopRun = useStopRun();
  const pauseRun = usePauseRun();
  const resumeRun = useResumeRun();
  const queryClient = useQueryClient();
  // Seed the target from the campaign's goal when it has one; fall back to a default.
  const initialLeads = campaign.goalTarget ?? DEFAULT_LEADS;
  const [leadSelection, setLeadSelection] = useState<number | 'custom'>(
    (LEAD_PRESETS as readonly number[]).includes(initialLeads) ? initialLeads : 'custom',
  );
  const [customLeads, setCustomLeads] = useState(String(initialLeads));
  const [capText, setCapText] = useState(String(DEFAULT_CAP_MINUTES));

  const inProcessRunning = selectIsCampaignRunning(run, campaign.id);
  const isAnyActive = selectIsAnyRunActive(run);
  const isPaused = selectIsRunPaused(run);
  const lastRun = selectLastRunForCampaign(run, campaign.id);

  // A fleet-routed run does NOT populate the in-process RUN block. Its live run id is
  // exposed DB-derived on the campaign (survives a refresh); prefer that over the
  // transient start-response runId (lost on reload). In-process runs leave both null.
  const fleetRunId = campaign.fleetRunId ?? runCampaign.data?.runId ?? null;
  // Show the live run while it's in flight; otherwise the just-started fleet run, else
  // the most recent run's final activity. The bridge serves finished runs too and
  // reports finished=true, so the poller fetches once and stops — no wasted polling.
  const feedRunId = inProcessRunning
    ? (run.active?.id ?? null)
    : (fleetRunId ?? lastRun?.id ?? null);
  const { activity, isError: activityError } = useRunActivity(isOpen ? feedRunId : null);

  // A fleet run has no in-process RUN.active, so `inProcessRunning` is always false
  // for it — treat it as live off its DB-derived run id until its activity reports
  // finished (fleetJob status done/failed/interrupted → finished=true server-side).
  const isFleetRun = fleetRunId !== null;
  const fleetRunLive = isFleetRun && activity?.finished === false;
  const isRunning = inProcessRunning || fleetRunLive;
  // An in-process run ALWAYS wins over a (possibly stale) DB-derived fleetRunId: the
  // process-global RUN lock is authoritative, so only show the fleet view (Close-only
  // footer + "managed on the Fleet page" hint) when no in-process run owns this drawer.
  // Without this, an in-process run for a campaign that also has a leftover queued fleet
  // job would hide Stop/Pause and lie about where it runs.
  const showFleetView = fleetRunLive && !inProcessRunning;

  // The activity feed sees `finished` (one ≤2s poll) well before the campaigns poll
  // would drop run.active — without this the card stays stuck on "Running…" and the
  // drawer on the live/Stop view after the run already halted. Re-sync the RUN block
  // (campaigns owns it) + the dashboard ticker the instant the live run finishes.
  // `signalled` makes it fire once.
  const finishSignalled = useRef(false);
  useEffect(() => {
    // Gate on "a run of ours was in flight" — for a fleet run `isRunning` flips to
    // false the same render `finished` flips true (fleetRunLive depends on !finished),
    // so gate on inProcessRunning || isFleetRun, not the derived isRunning.
    const wasLive = inProcessRunning || isFleetRun;
    if (wasLive && activity?.finished && !finishSignalled.current) {
      finishSignalled.current = true;
      void queryClient.invalidateQueries({ queryKey: queryKeys.campaigns });
      void queryClient.invalidateQueries({ queryKey: queryKeys.dashboard });
    }
    if (!activity?.finished) finishSignalled.current = false;
  }, [inProcessRunning, isFleetRun, activity?.finished, queryClient]);

  // Reset the run mutation when the drawer closes so a stale success/error flag
  // never bleeds into the next open (it must not drive any persistent UI).
  useEffect(() => {
    if (!isOpen) runCampaign.reset();
  }, [isOpen, runCampaign]);

  // Readiness NARROWED to this campaign's platforms. The global banner asks unscoped and
  // therefore answers "some worker is online", which reads green for an instagram
  // campaign on a youtube-only fleet — the run is accepted, dispatched, and dies on the
  // box. Here the campaign is in scope, so this is the one place that can ask the
  // question the operator actually has. Only polled while the drawer is open.
  const { readiness: scopedReadiness } = useAgentReadiness(isOpen ? campaign.id : undefined);
  // `ready` is the server's own gate; an undefined readiness (still loading, or the
  // probe errored) must NOT block the button — this warns, it never gates. The server
  // remains the real gate, exactly as the RBAC mirror rule says of UI gating.
  const platformUnready = isOpen && scopedReadiness !== undefined && !scopedReadiness.ready;

  const leads = leadSelection === 'custom' ? Number.parseInt(customLeads, 10) : leadSelection;
  const leadsValid = Number.isInteger(leads) && leads >= 1 && leads <= MAX_LEADS;
  const capMinutes = Number.parseInt(capText, 10);
  const capValid = Number.isInteger(capMinutes) && capMinutes >= MIN_MINUTES && capMinutes <= MAX_MINUTES;
  const isValid = leadsValid && capValid;

  // Don't close on success: keep the drawer open so it transitions into the live
  // preview in place once /api/state reports the run active (that's the whole point
  // of "view activity"). Single-run lock (409) guards a double-fire in the gap.
  const onStart = () => {
    if (!isValid) return;
    runCampaign.mutate({
      campaignId: campaign.id, mode: 'live',
      targetLeadCount: leads, durationMinutes: capMinutes,
    });
  };

  const pillClass = (active: boolean) =>
    cn(
      'rounded-full px-3.5 py-1.5 text-xs font-bold transition active:scale-95',
      active ? 'bg-accent text-accent-ink' : 'bg-surface-2 text-text hover:bg-border',
    );

  const title = (
    <>
      <h2 className="truncate font-head text-lg font-bold">Run {campaign.name}</h2>
      <p className="text-xs font-medium text-text-faint">Live run · watches real reels, no posting</p>
    </>
  );

  const pauseBusy = pauseRun.isPending || resumeRun.isPending;
  // A fleet run is managed from the Fleet admin page — /api/run/stop is a no-op for it,
  // so we never render Stop/Pause (they'd silently do nothing). Show a Close instead.
  const fleetFooter = (
    <Button variant="ghost" onClick={onClose}>Close</Button>
  );
  const inProcessFooter = (
    <>
      {isPaused ? (
        <Button variant="ghost" onClick={() => { resumeRun.mutate(); }} disabled={pauseBusy}>
          {pauseBusy
            ? <Loader2 className="size-3.5 animate-spin" aria-hidden />
            : <Play className="size-3.5" aria-hidden />}
          Resume
        </Button>
      ) : (
        <Button variant="ghost" onClick={() => { pauseRun.mutate(); }} disabled={pauseBusy}>
          {pauseBusy
            ? <Loader2 className="size-3.5 animate-spin" aria-hidden />
            : <Pause className="size-3.5" aria-hidden />}
          Pause
        </Button>
      )}
      <Button variant="danger" onClick={() => { stopRun.mutate(); }} disabled={stopRun.isPending}>
        {stopRun.isPending ? <Loader2 className="size-3.5 animate-spin" aria-hidden /> : null}
        Stop run
      </Button>
    </>
  );
  const idleFooter = (
    <>
      <Button variant="ghost" onClick={onClose}>Cancel</Button>
      <Button onClick={onStart} disabled={!isValid || isAnyActive || runCampaign.isPending}>
        {runCampaign.isPending
          ? <Loader2 className="size-3.5 animate-spin" aria-hidden />
          : <Zap className="size-3.5" aria-hidden />}
        Start run
      </Button>
    </>
  );
  // Fleet runs: no in-process controls (Close only). In-process live runs: Pause/Stop.
  // Otherwise the launch form's Cancel/Start.
  const footer = isRunning ? (showFleetView ? fleetFooter : inProcessFooter) : idleFooter;

  return (
    <Drawer isOpen={isOpen} onClose={onClose} title={title} footer={footer}>
      {isRunning ? (
        <div className="space-y-3">
          {showFleetView ? (
            <p className="text-xs text-text-faint">
              This run is executing on a worker in your fleet — its status is shown below. Stopping and
              pausing fleet runs is managed from the Fleet admin page.
            </p>
          ) : isPaused ? (
            <p className="inline-flex items-center gap-1.5 text-xs font-bold text-warn">
              <Pause className="size-3.5" aria-hidden />
              Paused — resume below to continue from where it left off.
            </p>
          ) : (
            <p className="text-xs text-text-faint">
              Live preview of this run — you can also watch the browser window. Pause or stop it any time below.
            </p>
          )}
          <RunActivityFeed activity={activity} isError={activityError} />
          {!isFleetRun && stopRun.isError ? (
            <p className="text-xs font-medium text-danger">{errorMessage(stopRun.error)}</p>
          ) : null}
        </div>
      ) : (
        <div className="space-y-4">
          <div>
            <span className="text-xs font-bold uppercase tracking-wide text-text-faint">Stop after</span>
            <div className="mt-2 flex flex-wrap items-center gap-2">
              {LEAD_PRESETS.map((n) => (
                <button
                  key={n}
                  type="button"
                  onClick={() => { setLeadSelection(n); }}
                  className={pillClass(leadSelection === n)}
                >
                  {n}
                </button>
              ))}
              <button
                type="button"
                aria-label="Custom lead target"
                onClick={() => { setLeadSelection('custom'); }}
                className={pillClass(leadSelection === 'custom')}
              >
                Custom
              </button>
              <span className="text-xs font-medium text-text-faint">leads</span>
            </div>
          </div>

          {leadSelection === 'custom' ? (
            <div>
              <label htmlFor="run-custom-leads" className="text-xs font-medium text-text-muted">
                Leads (1–{MAX_LEADS})
              </label>
              <input
                id="run-custom-leads"
                type="number"
                min={1}
                max={MAX_LEADS}
                value={customLeads}
                onChange={(e) => { setCustomLeads(e.target.value); }}
                className="mt-1 w-full rounded-tile border border-border bg-surface px-3 py-2 text-sm tabular-nums outline-none focus:border-accent"
              />
              {!leadsValid ? (
                <p className="mt-1 text-xs font-medium text-danger">Enter a whole number from 1 to {MAX_LEADS}.</p>
              ) : null}
            </div>
          ) : null}

          <div>
            <label htmlFor="run-cap-minutes" className="text-xs font-bold uppercase tracking-wide text-text-faint">
              Safety cap (max time)
            </label>
            <input
              id="run-cap-minutes"
              type="number"
              min={MIN_MINUTES}
              max={MAX_MINUTES}
              value={capText}
              onChange={(e) => { setCapText(e.target.value); }}
              className="mt-1 w-full rounded-tile border border-border bg-surface px-3 py-2 text-sm tabular-nums outline-none focus:border-accent"
            />
            {!capValid ? (
              <p className="mt-1 text-xs font-medium text-danger">Enter minutes from {MIN_MINUTES} to {MAX_MINUTES}.</p>
            ) : (
              <p className="mt-1 text-xs text-text-faint">Stops no later than this, even if it hasn’t hit the target.</p>
            )}
          </div>

          <p className="text-xs text-text-faint">
            It watches reels until it finds your target number of leads — or the safety cap or the 9pm
            daily window, whichever comes first — then stops. You can stop it early from here.
          </p>

          {isAnyActive ? (
            <p className="text-xs font-medium text-warn">Another run is active — wait for it to finish.</p>
          ) : null}
          {platformUnready ? (
            <p className="text-xs font-medium text-warn">
              {scopedReadiness.detail
                ?? 'No worker is ready for this campaign’s platform.'}
            </p>
          ) : null}
          {runCampaign.isError ? <RunStartError error={runCampaign.error} /> : null}

          {feedRunId ? (
            <div className="border-t border-border pt-3">
              <div className="flex items-center justify-between gap-2">
                <span className="text-xs font-bold uppercase tracking-wide text-text-faint">
                  {fleetRunId ? 'This run (fleet)' : 'Last run'}
                </span>
                {lastRun && !fleetRunId ? (
                  <span className="truncate text-xs text-text-faint">{lastRun.summary || lastRun.outcome}</span>
                ) : null}
              </div>
              <div className="mt-2">
                <RunActivityFeed activity={activity} isError={activityError} />
              </div>
            </div>
          ) : (
            <p className="border-t border-border pt-3 text-xs text-text-faint">
              No runs yet. Start one above — the live preview (reels watched, comments scored, leads
              found) shows here while it runs.
            </p>
          )}
        </div>
      )}
    </Drawer>
  );
}
