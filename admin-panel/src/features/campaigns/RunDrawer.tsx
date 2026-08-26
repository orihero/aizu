import { useEffect, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { Loader2, Pause, Play, Zap } from 'lucide-react';
import { Button } from '@/shared/ui/Button';
import { Drawer } from '@/shared/ui/Drawer';
import { formatNumber } from '@/shared/lib/formatters';
import { queryKeys } from '@/shared/api/queryKeys';
import { ResultError, type AppError } from '@/shared/lib/result';
import { usePauseRun, useResumeRun, useRunCampaign, useStopRun } from '@/shared/hooks/useWriteMutations';
import { useRunActivity } from '@/shared/hooks/useRunActivity';
import { useAgentReadiness } from '@/shared/hooks/useAgentReadiness';
import { useSettings } from '@/shared/hooks/useSettings';
import { useCan } from '@/shared/hooks/useCan';
import {
  selectIsAnyRunActive,
  selectIsCampaignRunning,
  selectIsRunPaused,
} from '@/shared/selectors/campaigns';
import type { Billing, Campaign, RunBlock } from '@/shared/types/domain';
import { describeRunStartError } from './describeRunStartError';
import { RunActivityFeed } from './RunActivityFeed';

const DEFAULT_LEADS = 25;
const MAX_LEADS = 1000; // mirrors the server's MAX_RUN_LEAD_TARGET

const BILLING_PATH = '/settings/billing';

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : 'Something went wrong.';
}

/** react-query's mutation `.error` is `unknown`, but every write here goes through
 * `unwrap()`, which only ever throws a ResultError — recover the typed AppError so
 * describeRunStartError can branch on its `code`/`status`, not just its message text. */
function toAppError(error: unknown): AppError {
  return error instanceof ResultError
    ? error.appError
    : { kind: 'unknown', message: errorMessage(error) };
}

/**
 * What this org's plan allows a single run to ask for — the ONE bound this drawer has
 * to express, since asking for leads is now the only question it puts to the operator.
 *
 * Two separate numbers, and the tighter one wins: `maxRunLeads` is the largest target
 * ONE run may request, `leadsRemaining` is what is left of the billing period. The
 * server clamps to exactly this (`clamped = min(requested, remaining)`) and 402s once
 * the period is spent — this mirror exists so the operator sees the bound BEFORE
 * pressing Start rather than discovering it as a run that quietly stopped early.
 *
 * It is a SOFT bound on the run itself (E.6): the engine's stop condition is tested
 * per match inside a batch, so a run can overshoot by up to a batch. Copy says "up to
 * N", never "exactly N".
 */
interface PlanBounds {
  /** Largest target we will send, or null when billing is unknown (viewer/member, or a
   *  fetch still in flight) — the server stays the real gate either way. */
  readonly maxLeads: number | null;
  readonly leadsRemaining: number | null;
  readonly planName: string | null;
  /** The period allowance is spent: starting a run would 402. */
  readonly exhausted: boolean;
}

const UNKNOWN_BOUNDS: PlanBounds = {
  maxLeads: null, leadsRemaining: null, planName: null, exhausted: false,
};

function resolvePlanBounds(billing: Billing | undefined): PlanBounds {
  if (!billing) return UNKNOWN_BOUNDS;
  const remaining = Math.max(0, billing.leadCap - billing.leadsUsed);
  // `maxRunLeads` defaults to 0 on a pre-v27 bridge; read that as "not reported" and
  // fall back to the period cap rather than offering a run target of zero.
  const perRun = billing.maxRunLeads > 0 ? billing.maxRunLeads : billing.leadCap;
  const displayName = billing.tiers.find((t) => t.tier === billing.tier)?.displayName ?? billing.tier;
  return {
    maxLeads: perRun > 0 ? Math.min(perRun, remaining) : null,
    leadsRemaining: remaining,
    planName: displayName,
    exhausted: remaining <= 0,
  };
}

interface RunStartErrorProps {
  readonly error: unknown;
}

/** The run-start (POST /api/run) inline error. A plain conflict/validation failure just
 * shows the server's message; the 409 agent-not-ready gate points the operator at the
 * fix, and a billing 402 gets plain copy plus the upgrade link — never a bare
 * "Plan limit reached", which reads as a bug rather than a plan that ran out. */
function RunStartError({ error }: RunStartErrorProps) {
  const canFixAgent = useCan('fix_agent');
  const described = describeRunStartError(toAppError(error));
  return (
    <div className="space-y-1">
      <p className="text-xs font-medium text-danger">{described.message}</p>
      {described.detail ? (
        <p className="text-xs font-medium text-text-faint">{described.detail}</p>
      ) : null}
      {described.upgrade ? (
        <Link to={BILLING_PATH} className="text-xs font-bold text-brand hover:underline">
          Upgrade plan →
        </Link>
      ) : null}
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
 * Right-side drawer to launch (and stop) a live run for one campaign.
 *
 * ONE question: how many leads. Bounded by what is left of the plan's period
 * allowance, because that is the only number that can make the answer wrong. There is
 * deliberately no operator-facing time cap any more — a run is defined by what it must
 * FIND, not by how long it may look, and the engine loops discovery sessions until it
 * has them (the server still passes a 12h runaway guard the operator never picks). A
 * run can still end short: the seeded sources get swept, or the daytime window closes.
 * The run summary names which, so a shortfall is explained rather than silent.
 *
 * While this campaign's run is in flight the drawer shows its live progress and a Stop
 * control instead.
 */
export function RunDrawer({ campaign, run, isOpen, onClose }: RunDrawerProps) {
  const runCampaign = useRunCampaign();
  const stopRun = useStopRun();
  const pauseRun = usePauseRun();
  const resumeRun = useResumeRun();
  const queryClient = useQueryClient();
  // Seed the target from the campaign's goal when it has one; fall back to a default.
  const [leadsText, setLeadsText] = useState(String(campaign.goalTarget ?? DEFAULT_LEADS));

  // The drawer is reachable only through the card's Run button (`run_campaigns`), which
  // is the same owner/admin set as `view_billing` — so BILLING is always present here
  // for a user who can actually start a run, and its absence degrades to "unbounded"
  // rather than to a locked form.
  const { data: settings } = useSettings();
  const plan = resolvePlanBounds(settings?.BILLING);

  const inProcessRunning = selectIsCampaignRunning(run, campaign.id);
  const isAnyActive = selectIsAnyRunActive(run);
  const isPaused = selectIsRunPaused(run);

  // A fleet-routed run does NOT populate the in-process RUN block. Its live run id is
  // exposed DB-derived on the campaign (survives a refresh); prefer that over the
  // transient start-response runId (lost on reload). In-process runs leave both null.
  const fleetRunId = campaign.fleetRunId ?? runCampaign.data?.runId ?? null;
  // Only a run of OURS that is (or may still be) in flight. An idle drawer is the
  // launch question and nothing else, so there is no last-run feed to render and no
  // reason to fetch one — the run history on the campaign card is where a finished run
  // belongs. A just-started fleet run still needs its id: `finished` is how we learn it
  // ended, since a fleet run never populates the in-process RUN block.
  const feedRunId = inProcessRunning ? (run.active?.id ?? null) : fleetRunId;
  const { activity, isError: activityError } = useRunActivity(isOpen ? feedRunId : null);
  // An in-process run's clamped target never reaches /api/run/activity (only a fleet
  // job carries it durably in its spec), so pass the one the start response echoed back
  // — otherwise the progress block reads "3 leads found" with no denominator.
  const startedTarget = runCampaign.data?.targetLeads ?? null;

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

  const requested = Number.parseInt(leadsText, 10);
  const requestedValid = Number.isInteger(requested) && requested >= 1 && requested <= MAX_LEADS;
  // What we actually send. Clamping here (rather than rejecting the input) is what makes
  // the default "just press Start" path correct on a small plan: the server would clamp
  // to the same number anyway, and a validation error would leave a free user staring at
  // a form they have to solve before they can run anything.
  const leads = plan.maxLeads !== null && requestedValid
    ? Math.min(requested, plan.maxLeads)
    : requested;
  const clampedByPlan = requestedValid && plan.maxLeads !== null && requested > plan.maxLeads;
  // The bound the field advertises: what is left of the plan's period allowance when
  // billing is known, otherwise the global safety cap. Never larger than MAX_LEADS.
  const leadsMax = plan.maxLeads === null ? MAX_LEADS : Math.min(MAX_LEADS, plan.maxLeads);
  const isValid = requestedValid && !plan.exhausted;

  // ONE line under the field, not two. It carries the plan bound and the clamp in the
  // same breath — a separate warning would repeat the same number back at the operator,
  // which is exactly the noise this drawer was cut down to remove. Built as a string so
  // it lands in the DOM as one text node rather than a run of fragments.
  const planSuffix = plan.planName ? ` on ${plan.planName}` : '';
  const planNote = plan.leadsRemaining === null
    ? 'It keeps searching until it finds them.'
    : clampedByPlan
      ? `Only ${formatNumber(plan.maxLeads)} left${planSuffix} — we’ll start it with that.`
      : `${formatNumber(plan.leadsRemaining)} left this period${planSuffix}. It keeps `
        + 'searching until it finds them.';

  // Don't close on success: keep the drawer open so it transitions into the live
  // preview in place once /api/state reports the run active (that's the whole point
  // of "view activity"). Single-run lock (409) guards a double-fire in the gap.
  const onStart = () => {
    if (!isValid) return;
    // No durationMinutes: the run is bounded by its lead target, not by wall clock. The
    // server supplies its own runaway guard, which is not an operator's decision to make.
    runCampaign.mutate({ campaignId: campaign.id, mode: 'live', targetLeadCount: leads });
  };

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
              Live progress for this run — you can also watch the browser window. Pause or stop it any time below.
            </p>
          )}
          <RunActivityFeed
            activity={activity}
            isError={activityError}
            targetLeadsHint={startedTarget}
          />
          {!isFleetRun && stopRun.isError ? (
            <p className="text-xs font-medium text-danger">{errorMessage(stopRun.error)}</p>
          ) : null}
        </div>
      ) : (
        <div className="space-y-4">
          <div>
            <label htmlFor="run-leads" className="text-xs font-bold uppercase tracking-wide text-text-faint">
              How many leads?
            </label>
            <input
              id="run-leads"
              type="number"
              min={1}
              max={leadsMax}
              value={leadsText}
              onChange={(e) => { setLeadsText(e.target.value); }}
              className="mt-1 w-full rounded-tile border border-border bg-surface px-3 py-2 text-base tabular-nums outline-none focus:border-accent"
            />
            {/* The field ADVERTISES the bound (spinner + browser validity agree with what
                Start sends), but `max` on a number input cannot stop someone typing past
                it, so an over-plan entry still CLAMPS rather than blocking the form — the
                one-button path is the whole point of this drawer. */}
            {!requestedValid ? (
              <p className="mt-1 text-xs font-medium text-danger">
                Enter a whole number from 1 to {formatNumber(leadsMax)}.
              </p>
            ) : (
              <p className={clampedByPlan
                ? 'mt-1 text-xs font-medium text-warn'
                : 'mt-1 text-xs text-text-faint'}>
                {planNote}
              </p>
            )}
          </div>

          {/* The period allowance is spent: the server answers 402 to any start. Say so
              here, with the way out, rather than letting the operator discover it by
              pressing a button that fails. */}
          {plan.exhausted ? (
            <div className="space-y-1">
              <p className="text-xs font-medium text-warn">
                You’ve used all {formatNumber(settings?.BILLING?.leadCap ?? 0)} leads
                {plan.planName ? ` on ${plan.planName}` : ''} this billing period, so runs are paused
                until it resets.
              </p>
              <Link to={BILLING_PATH} className="text-xs font-bold text-brand hover:underline">
                Upgrade plan →
              </Link>
            </div>
          ) : null}
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
        </div>
      )}
    </Drawer>
  );
}
