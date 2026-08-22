// AIZU Worker — Operator Command Center controller (thin UI, Phase 6).
//
// Vanilla JS. NO business logic: it subscribes to Rust-emitted events, renders them, and
// forwards button clicks to Tauri commands. ALL state truth is the `status-updated` event
// (the Rust side polls the sidecar's loopback GET /status). This file NEVER scrapes logs
// for STATE — the ONE log-derived signal is the 2FA checkpoint EMPHASIS heuristic, which
// only escalates the visual weight of an always-available Focus-Chrome button.
//
// Charts are hand-drawn inline SVG (document.createElementNS) built from a ROLLING
// IN-MEMORY BUFFER of the SAME event stream. Buffers live at module scope, reset on every
// relaunch (page reload), and are NEVER persisted / NEVER fabricated. Empty/warming states
// are mandatory until a series has >= 2 real points.
//
// Tauri 2.x exposes the API on `window.__TAURI__` (withGlobalTauri:true). We defensively
// resolve `invoke`/`listen` so a missing global (plain browser) fails SOFT and the whole
// dashboard still renders in warming/empty states.

(function () {
  "use strict";

  // -------------------------------------------------------------------- Tauri bridge
  const tauri = window.__TAURI__ || {};
  const invoke = tauri.core && tauri.core.invoke
    ? tauri.core.invoke
    : async () => { throw new Error("Tauri API unavailable (open inside the app)"); };
  const listen = tauri.event && tauri.event.listen
    ? tauri.event.listen
    : async () => () => {};

  // -------------------------------------------------------------------- constants
  const STALE_MS = 5000;            // status snapshot age before we show "stale"
  const MAX_LOG_LINES = 1000;       // log-pane DOM cap
  const MAX_INSTALL_LINES = 200;    // browser-download progress-pane DOM cap
  const BUFFER_MAX_TICKS = 180;     // 3 min at the unified 1s sample cadence; FIFO
  const MAX_JOB_HISTORY = 500;      // cap distinct-jobId tracking on an always-on box
  const LIVE_DOT_SILENCE_MS = 3000; // log live-dot goes quiet after this
  const CHECKPOINT_REARM_MS = 20000;
  const SVG_NS = "http://www.w3.org/2000/svg";
  const LOCAL_DISPATCH = "http://127.0.0.1:8765";
  /// The highest wizard step index (7 = Verify). Steps 5/6 drop out on an API-only box.
  const WZ_LAST_STEP = 7;

  // Log substrings that hint a human is needed (case-insensitive; emphasis only).
  const CHECKPOINT_CUES = [
    "2fa", "two-factor", "verification code", "captcha",
    "checkpoint", "challenge_required", "confirm it's you",
  ];

  // -------------------------------------------------------------------- element handles
  const el = (id) => document.getElementById(id);
  const nodes = {
    // spine target is document.body via CSS var
    connBadge: el("conn-badge"), connDot: el("conn-dot"),
    workerId: el("worker-id"), envChip: el("env-chip"), dispatchHost: el("dispatch-host"),
    btnPause: el("btn-pause"), btnResume: el("btn-resume"), leaseNote: el("lease-note"),
    btnStop: el("btn-stop"), btnRestart: el("btn-restart"),
    capacity: el("capacity"), capReadout: el("cap-readout"),
    capMinus: el("cap-minus"), capPlus: el("cap-plus"), btnCapacity: el("btn-capacity"),
    checkpointBlock: el("checkpoint-block"), checkpointSub: el("checkpoint-sub"), btnFocus: el("btn-focus"),
    // hero
    heroBand: el("hero-band"), heroCalm: el("hero-calm"), heroTakeover: el("hero-takeover"),
    heroDot: el("hero-dot"), heroFocus: el("hero-focus"), heroFocusBig: el("hero-focus-big"),
    sessionUptimeHero: el("session-uptime-hero"), heroJobs: el("hero-jobs"),
    heroTakeoverSub: el("hero-takeover-sub"),
    updateStrip: el("update-strip"), reenrolStrip: el("reenrol-strip"),
    // preflight health strip (steady state)
    shellStrip: el("shell-strip"), shellStripText: el("shell-strip-text"),
    pfStrip: el("pf-strip"), pfStripPill: el("pf-strip-pill"), pfStripText: el("pf-strip-text"),
    pfStripBody: el("pf-strip-body"), pfStripToggle: el("pf-strip-toggle"),
    // KPI
    workerDot: el("worker-dot"), workerWord: el("worker-word"), workerSub: el("worker-sub"),
    chromeBadge: el("chrome-badge"), chromeVersion: el("chrome-version"),
    acctBusy: el("acct-busy"), acctTotal: el("acct-total"), acctMiniBar: el("acct-mini-bar"),
    jobDot: el("job-dot"), jobState: el("job-state"), kpiRunDuration: el("kpi-run-duration"),
    sessionUptime: el("session-uptime"), jobsCount: el("jobs-count"),
    // now playing
    npState: el("np-state"), jobDetail: el("job-detail"), runDuration: el("run-duration"),
    npBusyBar: el("np-busy-bar"), npFooter: el("np-footer"),
    // charts
    svgDonut: el("svg-donut"), legendBusy: el("legend-busy"), legendIdle: el("legend-idle"),
    svgPlatforms: el("svg-platforms"),
    svgActivity: el("svg-activity"), activityReadout: el("activity-readout"),
    svgThroughput: el("svg-throughput"), throughputReadout: el("throughput-readout"),
    svgUptime: el("svg-uptime"), uptimeReadout: el("uptime-readout"),
    // log console
    logConsole: el("log-console"), main: el("main"), logDot: el("log-dot"),
    logPath: el("log-path"), logPane: el("log-pane"),
    btnFollowTail: el("btn-follow-tail"), btnCopyPath: el("btn-copy-path"), btnLogExpand: el("btn-log-expand"),
    copyBuffer: el("copy-buffer"),
    // counters
    ctJobs: el("ct-jobs"), ctUptime: el("ct-uptime"), ctRun: el("ct-run"),
    ctRestarts: el("ct-restarts"), ctPoll: el("ct-poll"),
    // advanced (ex-"dev") menu
    overlay: el("dev-overlay"), devDispatch: el("dev-dispatch"), devPort: el("dev-port"),
    devToken: el("dev-token"), devTokenState: el("dev-token-state"), devMsg: el("dev-msg"),
    devCloudNote: el("dev-cloud-note"),
    // setup wizard
    wzOverlay: el("wizard-overlay"), wzSteps: el("wz-steps"), wzBody: el("wz-body"),
    wzMsg: el("wz-msg"), wzBack: el("wz-back"), wzNext: el("wz-next"), wzLater: el("wz-later"),
    btnSetup: el("btn-setup"), btnSetupLabel: el("btn-setup-label"),
  };

  // -------------------------------------------------------------------- rolling buffers
  // All plain module-scope collections; reset to empty on relaunch. Never persisted.
  const buffers = {
    busyRatio: [],   // busy/total sampled per 1s tick -> Activity area sparkline
    logRate: [],     // non-seeded lines per ~1s bucket -> Throughput bars
    conn: [],        // 1 if a status arrived within STALE_MS of the tick else 0
  };
  const jobIds = new Set();            // distinct currentJob.jobId -> jobs/session
  const firstSeenAt = new Map();       // jobId -> epochMs (for run-duration)

  const state = {
    lastStatusAt: 0,
    sessionStartAt: 0,
    lastStatus: null,
    lastSidecar: null,
    prevSidecarState: null,
    restartCount: 0,
    distinctJobs: 0,   // monotonic lifetime count (jobIds Set is a capped working set)
    lastLogAt: 0,
    currentJobId: null,
    logRateBucket: 0,
    checkpointLatched: false,
    checkpointRearmAt: 0,
    lastAppliedCapacity: 1,
    followTail: true,
    dispatchIsCloud: false,
  };

  // ==================================================================== command wiring
  // On failure: flash the CLICKED button (not the connection badge) and console.warn.
  function flashError(node, err) {
    console.warn("command failed:", err);
    if (!node) return;
    node.classList.add("cmd-error");
    setTimeout(() => node.classList.remove("cmd-error"), 400);
  }
  const wire = (id, fn) => {
    const node = el(id);
    if (node) node.addEventListener("click", () => { try { fn(node); } catch (e) { flashError(node, e); } });
  };
  const cmd = (name, args) => (node) => invoke(name, args).catch((e) => flashError(node, e));

  wire("btn-pause", cmd("pause"));
  wire("btn-resume", cmd("resume"));
  wire("btn-stop", cmd("stop_current_job"));
  wire("btn-restart", cmd("restart_sidecar"));
  wire("btn-focus", (node) => { dismissCheckpoint(); return cmd("focus_chrome")(node); });
  wire("hero-focus", (node) => { dismissCheckpoint(); return cmd("focus_chrome")(node); });
  wire("hero-focus-big", (node) => { dismissCheckpoint(); return cmd("focus_chrome")(node); });
  wire("btn-capacity", (node) => {
    const cap = clampCapacity(nodes.capacity.value);
    return invoke("set_capacity_override", { capacity: cap })
      .then(() => { state.lastAppliedCapacity = cap; syncCapacityControls(); })
      .catch((e) => flashError(node, e));
  });

  // capacity slider / steppers (pure UI; no state emitted until Set)
  function syncCapacityControls() {
    const v = clampCapacity(nodes.capacity.value);
    nodes.capReadout.textContent = String(v);
    nodes.capacity.classList.toggle("is-zero", v === 0);
    const changed = v !== state.lastAppliedCapacity;
    nodes.btnCapacity.disabled = !changed;
    nodes.btnCapacity.setAttribute("aria-disabled", String(!changed));
  }
  if (nodes.capacity) nodes.capacity.addEventListener("input", syncCapacityControls);
  if (nodes.capMinus) nodes.capMinus.addEventListener("click", () => {
    nodes.capacity.value = clampCapacity(parseInt(nodes.capacity.value, 10) - 1); syncCapacityControls();
  });
  if (nodes.capPlus) nodes.capPlus.addEventListener("click", () => {
    nodes.capacity.value = clampCapacity(parseInt(nodes.capacity.value, 10) + 1); syncCapacityControls();
  });
  syncCapacityControls();

  // -------------------------------------------------------------------- log console UI
  if (nodes.btnFollowTail) nodes.btnFollowTail.addEventListener("click", () => setFollowTail(!state.followTail));
  if (nodes.btnCopyPath) nodes.btnCopyPath.addEventListener("click", copyLogPath);
  if (nodes.btnLogExpand) nodes.btnLogExpand.addEventListener("click", toggleLogExpand);
  if (nodes.logPane) nodes.logPane.addEventListener("scroll", onLogScroll);

  function setFollowTail(on) {
    state.followTail = on;
    nodes.btnFollowTail.classList.toggle("is-on", on);
    nodes.btnFollowTail.setAttribute("aria-pressed", String(on));
    nodes.btnFollowTail.textContent = on ? "follow ●" : "follow ○";
    if (on) nodes.logPane.scrollTop = nodes.logPane.scrollHeight;
  }
  function onLogScroll() {
    const p = nodes.logPane;
    const atBottom = p.scrollHeight - p.scrollTop - p.clientHeight < 8;
    if (atBottom && !state.followTail) setFollowTail(true);
    else if (!atBottom && state.followTail) setFollowTail(false);
  }
  function toggleLogExpand() {
    const on = nodes.main.classList.toggle("log-expanded");
    nodes.btnLogExpand.setAttribute("aria-pressed", String(on));
  }
  function copyLogPath() {
    const path = nodes.logPath.getAttribute("data-full") || nodes.logPath.textContent || "";
    if (!path || path === "—") return;
    nodes.copyBuffer.value = path;
    nodes.copyBuffer.select();
    try { document.execCommand("copy"); } catch (e) { console.warn("copy failed:", e); }
    nodes.btnCopyPath.textContent = "copied";
    setTimeout(() => { nodes.btnCopyPath.textContent = "copy"; }, 1200);
  }

  // ==================================================================== advanced menu
  // The 7-tap-the-logo easter egg that used to reveal this is GONE. It was the only
  // configuration path in the app, which meant a freshly installed worker opened a dead
  // dashboard with no visible way to fix it. Setup is now a labelled rail button and this
  // menu is a plainly-named escape hatch for switching dispatch environments.
  wire("btn-advanced", () => openDevMenu());

  async function openDevMenu() {
    nodes.devMsg.textContent = "";
    nodes.devToken.value = "";
    try {
      const cfg = await invoke("get_config");
      nodes.devDispatch.value = (cfg && cfg.dispatchBaseUrl) || "";
      nodes.devPort.value = (cfg && cfg.controlPort) || 8788;
      nodes.devTokenState.textContent = cfg && cfg.hasToken ? "· stored" : "· none";
    } catch (e) {
      nodes.devTokenState.textContent = "";
    }
    updateDevCloudNote();
    if (nodes.overlay) nodes.overlay.classList.remove("hidden");
    if (nodes.devDispatch) nodes.devDispatch.focus();
  }
  function closeDevMenu() { if (nodes.overlay) nodes.overlay.classList.add("hidden"); }

  function updateDevCloudNote() {
    const url = (nodes.devDispatch.value || "").trim();
    const loopback = /^https?:\/\/(127\.0\.0\.1|localhost)(:|\/|$)/i.test(url);
    nodes.devCloudNote.classList.toggle("hidden", !url || loopback);
  }
  if (nodes.devDispatch) nodes.devDispatch.addEventListener("input", updateDevCloudNote);

  wire("dev-close", () => closeDevMenu());
  wire("preset-local", () => { nodes.devDispatch.value = LOCAL_DISPATCH; updateDevCloudNote(); });
  wire("preset-cloud", () => {
    if (!/^https?:\/\//.test(nodes.devDispatch.value)) nodes.devDispatch.value = "https://";
    updateDevCloudNote();
    nodes.devDispatch.focus();
  });
  wire("dev-save", (node) => {
    nodes.devMsg.textContent = "saving…";
    const args = {
      dispatchBaseUrl: nodes.devDispatch.value.trim(),
      controlPort: clampPort(nodes.devPort.value),
      bootstrapToken: nodes.devToken.value ? nodes.devToken.value : null,
    };
    // save_config now restarts the SIDECAR in place (it used to relaunch the whole app),
    // so this resolves and we can report success instead of vanishing.
    return invoke("save_config", args)
      .then(() => {
        nodes.devMsg.textContent = "saved — worker restarting";
        nodes.devToken.value = "";
        return refreshSetup();
      })
      .catch((e) => {
        nodes.devMsg.textContent = errText(e);
        flashError(node, e);
      });
  });

  // Scrim click + Esc close the advanced overlay. The WIZARD is deliberately NOT
  // Esc-closable from the scrim: leaving it is an explicit act (✕ / "Close and fix later"),
  // because that act is what records the deferral — see dismissWizard().
  if (nodes.overlay) nodes.overlay.addEventListener("click", (e) => { if (e.target === nodes.overlay) closeDevMenu(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDevMenu(); });
  function applyDispatchConfig(url) {
    if (!url) { nodes.dispatchHost.textContent = "—"; return; }
    let host = url;
    try { host = new URL(url).host || url; } catch (e) { /* keep raw */ }
    nodes.dispatchHost.textContent = host;
    nodes.dispatchHost.title = url;
    const loopback = /^(127\.0\.0\.1|localhost)(:|$)/i.test(host);
    state.dispatchIsCloud = !loopback;
    nodes.envChip.textContent = loopback ? "Local" : "Cloud";
    nodes.envChip.classList.toggle("is-cloud", !loopback);
  }

  // ==================================================================== event subscriptions
  // listen() returns a Promise<unlisten>; guard it so a transient IPC hiccup during startup
  // becomes a console warning, not an unhandled rejection.
  const safeListen = (name, fn) =>
    listen(name, fn).catch((e) => console.warn("listen failed:", name, e));
  safeListen("status-updated", (evt) => onStatus(evt.payload));
  safeListen("sidecar-status", (evt) => onSidecar(evt.payload));
  safeListen("log-line", (evt) => onLog(evt.payload));
  safeListen("chrome-install-progress", (evt) => onChromeInstallProgress(evt.payload));

  // ==================================================================== status handler
  function onStatus(s) {
    if (!s) return;
    const now = Date.now();
    if (!state.sessionStartAt) state.sessionStartAt = now;
    state.lastStatusAt = now;
    state.lastStatus = s;

    // --- snapshot for KPI render (tick BUFFERS are sampled on the 1s cadence below, NOT
    //     per-event, so busyRatio / conn / logRate all share one wall-clock x-axis) ---
    const accounts = s.accounts || [];
    const total = accounts.length;
    const busy = accounts.filter((a) => a.status === "busy").length;

    // job tracking
    const job = s.currentJob || null;
    const jobId = job && job.jobId ? String(job.jobId) : null;
    if (jobId) {
      if (!jobIds.has(jobId)) {
        jobIds.add(jobId);
        firstSeenAt.set(jobId, now);
        state.distinctJobs += 1;
        // FIFO-evict the oldest so an always-on box can't grow these unbounded (Set/Map
        // preserve insertion order; the KPI is "distinct jobs seen this session").
        if (jobIds.size > MAX_JOB_HISTORY) {
          const oldest = jobIds.values().next().value;
          jobIds.delete(oldest);
          firstSeenAt.delete(oldest);
        }
      }
      state.currentJobId = jobId;
    } else {
      state.currentJobId = null;
    }

    // checkpoint latch clears if the job changed/ended
    if (state.checkpointLatched && state.currentJobId !== state._latchedJobId) clearCheckpoint();

    renderConnection("connected");
    renderWorkerTile(state.lastSidecar);
    renderChrome(s.chrome);
    renderAccountsKpi(busy, total);
    renderJob(job, s.controls || {});
    renderControls(s.controls || {}, job);
    renderCheckpointClearOnChrome(s.chrome);
    // The preflight rides the SAME snapshot — no extra IPC for the strip. `undefined`
    // (an older sidecar that never sends the key) and `null` (first pass still running)
    // both render as "checking…", never as healthy.
    renderPreflightStrip(s.preflight);
    // …and the SHELL's own bad news beside it. Cheap enough to ride this beat: it is a lock
    // read in Rust, not the loopback round-trip `get_setup_state` costs.
    refreshShellNote();
    // While the wizard is open its step must track the live report, so re-derive it on the
    // same 1.5s beat the status poller already runs on.
    if (setup.open) refreshSetup();
    scheduleChartRender();
  }

  // ==================================================================== sidecar handler
  function onSidecar(st) {
    if (!st) return;
    state.lastSidecar = st;
    // Count transitions INTO crashed/backing_off as restarts.
    if ((st.state === "crashed" || st.state === "backing_off") && state.prevSidecarState !== st.state) {
      state.restartCount += 1;
    }
    state.prevSidecarState = st.state;
    renderWorkerTile(st);
    // Draw the operator to Restart when crashed.
    nodes.btnRestart.classList.toggle("pulse-outline", st.state === "crashed");
  }

  // ==================================================================== log handler
  function onLog(batch) {
    if (!batch) return;
    state.lastLogAt = Date.now();
    nodes.logDot.classList.add("is-live");
    nodes.logPath.textContent = shortenPath(batch.path);
    nodes.logPath.setAttribute("data-full", batch.path || "");
    nodes.logPath.title = batch.path || "";

    const lines = batch.lines || [];
    if (batch.seeded) {
      nodes.logPane.textContent = "";
    } else {
      // Only LIVE (non-seeded) volume feeds throughput; seeded is a backfill dump.
      state.logRateBucket += lines.length;
      maybeLatchCheckpoint(lines);
    }
    if (lines.length) appendLog(lines);
  }

  function appendLog(lines) {
    let text = nodes.logPane.textContent;
    if (text === "Waiting for the current job's log…") text = "";
    for (const line of lines) text += line + "\n";
    const arr = text.split("\n");
    if (arr.length > MAX_LOG_LINES) text = arr.slice(arr.length - MAX_LOG_LINES).join("\n");
    nodes.logPane.textContent = text;
    if (state.followTail) nodes.logPane.scrollTop = nodes.logPane.scrollHeight;
  }

  // ==================================================================== 2FA checkpoint (emphasis only)
  function maybeLatchCheckpoint(lines) {
    if (!state.currentJobId) return;
    if (Date.now() < state.checkpointRearmAt) return;
    const hay = lines.join("\n").toLowerCase();
    if (CHECKPOINT_CUES.some((c) => hay.includes(c))) latchCheckpoint();
  }
  function latchCheckpoint() {
    state.checkpointLatched = true;
    state._latchedJobId = state.currentJobId;
    nodes.checkpointBlock.classList.add("is-alert");
    nodes.heroCalm.classList.add("hidden");
    nodes.heroTakeover.classList.remove("hidden");
    nodes.heroBand.classList.add("is-takeover");
    const s = state.lastStatus;
    const job = s && s.currentJob;
    const handle = describeCheckpointTarget(s, job);
    nodes.heroTakeoverSub.textContent = handle;
  }
  function clearCheckpoint() {
    state.checkpointLatched = false;
    state._latchedJobId = null;
    nodes.checkpointBlock.classList.remove("is-alert");
    nodes.heroTakeover.classList.add("hidden");
    nodes.heroCalm.classList.remove("hidden");
    nodes.heroBand.classList.remove("is-takeover");
  }
  function dismissCheckpoint() {
    // Optimistic dismiss when the operator clicks Focus Chrome; re-arm after a window.
    if (!state.checkpointLatched) return;
    state.checkpointRearmAt = Date.now() + CHECKPOINT_REARM_MS;
    clearCheckpoint();
  }
  function renderCheckpointClearOnChrome(chrome) {
    // If Chrome (re)connects while latched, treat the checkpoint as handled.
    if (state.checkpointLatched && chrome && chrome.connected) clearCheckpoint();
  }
  function describeCheckpointTarget(s, job) {
    const plat = job && job.platform ? platformLabel(job.platform) : null;
    const accts = (s && s.accounts) || [];
    const acct = accts.find((a) => a.status === "busy") || accts[0];
    const handle = acct && acct.accountHandle ? acct.accountHandle : "(managed account)";
    if (plat) return plat + " " + handle + " waiting on 2FA / captcha.";
    return "Waiting on 2FA / captcha.";
  }

  // ==================================================================== renderers
  function renderConnection(mode) {
    // mode: "connected" | "connecting" | "stale"
    if (mode === "connected") {
      nodes.connBadge.textContent = "connected";
      nodes.connDot.className = "live-dot is-live";
      setSpine("connected");
    } else if (mode === "stale") {
      const age = Math.round((Date.now() - state.lastStatusAt) / 1000);
      nodes.connBadge.textContent = "stale (" + age + "s)";
      nodes.connDot.className = "live-dot is-warn";
      setSpine("stale");
    } else {
      nodes.connBadge.textContent = "connecting…";
      nodes.connDot.className = "live-dot";
      setSpine("connecting");
    }
    nodes.workerId.textContent = (state.lastStatus && state.lastStatus.workerId) || "—";
    nodes.workerId.title = (state.lastStatus && state.lastStatus.workerId) || "";
  }

  // Every state the supervisor actually emits. It emits "running", NOT "healthy" — the
  // old table only matched "healthy", so a perfectly running worker sat on "connecting"
  // forever, which is precisely the kind of silence this work exists to remove.
  const SIDECAR_WORDS = {
    starting:       ["starting",  "live-dot sm is-warn",    "launching the worker process"],
    running:        ["healthy",   "live-dot sm is-success", "process"],
    backing_off:    ["restarting", "live-dot sm is-warn",   "backing off"],
    crashed:        ["crashed",   "live-dot sm is-danger",  "restart to recover"],
    stopping:       ["stopping",  "live-dot sm is-warn",    "shutting down"],
    stopped:        ["stopped",   "live-dot sm is-warn",    "not running"],
    not_configured: ["not set up", "live-dot sm is-warn",   "open Setup to finish"],
  };

  function renderWorkerTile(st) {
    if (!st) { nodes.workerWord.textContent = "connecting"; nodes.workerDot.className = "live-dot sm"; return; }
    const row = SIDECAR_WORDS[st.state];
    if (!row) return;
    nodes.workerWord.textContent = row[0];
    nodes.workerDot.className = row[1];
    let sub = row[2];
    if (st.state === "backing_off" && st.restartInSec != null) {
      sub += " · " + st.restartInSec.toFixed(1) + "s";
    }
    // A spawn failure (missing binary, permission denied) carries its reason here. It used
    // to be an eprintln! on a machine with no terminal.
    if (st.detail) sub = st.detail;
    nodes.workerSub.textContent = sub;
    nodes.workerSub.title = st.detail || "";
  }

  function renderChrome(chrome) {
    if (chrome) {
      const ok = !!chrome.connected;
      nodes.chromeBadge.textContent = ok ? "attached" : "not attached";
      nodes.chromeBadge.className = "pill " + (ok ? "pill-success" : "pill-danger");
      nodes.chromeVersion.textContent = chrome.browserVersion || shortenUrl(chrome.cdpUrl) || "—";
      nodes.chromeVersion.title = chrome.cdpUrl || "";
    } else {
      nodes.chromeBadge.textContent = "unknown";
      nodes.chromeBadge.className = "pill pill-warn";
      nodes.chromeVersion.textContent = "—";
    }
  }

  function renderAccountsKpi(busy, total) {
    nodes.acctBusy.textContent = String(busy);
    nodes.acctTotal.textContent = String(total);
    // 5-cell mini stacked bar: fill = round(busy/total*5).
    const on = total ? Math.round((busy / total) * 5) : 0;
    nodes.acctMiniBar.innerHTML = "";
    for (let i = 0; i < 5; i++) {
      const span = document.createElement("span");
      if (i < on) span.className = "on";
      nodes.acctMiniBar.appendChild(span);
    }
  }

  function renderJob(job, controls) {
    // KPI tile 4 word + Now-playing pill share the same logic (worst state wins).
    let cls = "pill pill-neutral", word = controls.drain ? "draining" : "idle";
    let live = false;
    // A revoked box (B10) is terminal — it outranks every other word, including a live
    // job's, because nothing it does now will reach the cloud.
    if (controls.reenrolmentRequired) { cls = "pill pill-danger"; word = "revoked"; }
    else if (controls.halt) { cls = "pill pill-danger"; word = controls.haltReason || "halting"; }
    else if (controls.paused) { cls = "pill pill-warn"; word = "paused"; }
    else if (job) { cls = "pill pill-live"; word = job.status || "running"; live = true; }
    else if (controls.drain) { cls = "pill pill-info"; word = "draining"; }

    nodes.jobState.textContent = word; nodes.jobState.className = cls;
    nodes.npState.textContent = word; nodes.npState.className = cls;
    nodes.jobDot.className = "live-dot sm" + (live ? " is-live" : "");

    // Now-playing body
    if (job) {
      nodes.jobDetail.innerHTML =
        escapeHtml(platformLabel(job.platform)) +
        " · campaign <span class='mono'>" + escapeHtml(job.campaignId) + "</span>";
      const footer = [];
      if (job.jobId) footer.push("job " + escapeHtml(job.jobId));
      if (job.runId) footer.push("run " + escapeHtml(job.runId));
      nodes.npFooter.innerHTML = footer.join(" · ");
    } else {
      const reason = controls.reenrolmentRequired ? "token revoked — re-enrolment required"
        : controls.halt ? "halted" : controls.paused ? "paused" : controls.drain ? "draining" : null;
      nodes.jobDetail.textContent = reason ? "No job leasing (" + reason + ")." : "No job leasing right now.";
      nodes.npFooter.textContent = "";
    }

    // busy-ratio progress bar (share of session ticks with any account busy)
    const withData = buffers.busyRatio.length;
    const busyTicks = buffers.busyRatio.filter((r) => r > 0).length;
    const pct = withData ? Math.round((busyTicks / withData) * 100) : 0;
    nodes.npBusyBar.firstElementChild.style.width = pct + "%";
  }

  function renderControls(controls, job) {
    // Segmented pause/resume reflects controls.paused.
    const paused = !!controls.paused;
    nodes.btnPause.classList.toggle("is-active", paused);
    nodes.btnPause.classList.toggle("is-paused", paused);
    nodes.btnResume.classList.toggle("is-active", !paused);
    nodes.leaseNote.textContent = paused
      ? "Not leasing new jobs."
      : controls.drain ? "Draining — will not lease new jobs." : "Leasing new jobs.";

    // Stop enabled only with a live job.
    const hasJob = !!job;
    nodes.btnStop.disabled = !hasJob;
    nodes.btnStop.setAttribute("aria-disabled", String(!hasJob));

    // Leasing controls (pause/resume) disabled under halt, except Restart.
    const halted = !!controls.halt;
    nodes.btnPause.disabled = halted;
    nodes.btnResume.disabled = halted;

    // update-required strip
    nodes.updateStrip.classList.toggle("hidden", !controls.updateRequired);
    // revoked-token strip (B10). Pause/Resume are pointless once the pull loop has
    // stopped for good, so they are disabled alongside the halt case above.
    const revoked = !!controls.reenrolmentRequired;
    nodes.reenrolStrip.classList.toggle("hidden", !revoked);
    if (revoked) {
      nodes.btnPause.disabled = true;
      nodes.btnResume.disabled = true;
      nodes.leaseNote.textContent = "Token revoked — not leasing until re-enrolled.";
    }
  }

  // ==================================================================== spine (worst-condition wins)
  function setSpine(kind) {
    const s = state.lastStatus, c = (s && s.controls) || {};
    const sd = state.lastSidecar;
    let color = "var(--info)"; // connecting default
    if (sd && sd.state === "crashed") color = "var(--danger)";
    else if (c.reenrolmentRequired) color = "var(--danger)";  // B10: revoked box
    else if (c.halt) color = "var(--danger)";
    else if ((sd && sd.state === "backing_off") || c.paused || c.drain) color = "var(--warn)";
    else if (kind === "connecting") color = "var(--info)";
    else if (kind === "stale") color = "var(--warn)";
    else if (kind === "connected") color = "var(--success)";
    document.body.style.setProperty("--rail", color);
  }

  // ==================================================================== chart render (rAF-throttled)
  let rafPending = false;
  function scheduleChartRender() {
    if (rafPending) return;
    rafPending = true;
    requestAnimationFrame(() => { rafPending = false; renderCharts(); });
  }
  function renderCharts() {
    const s = state.lastStatus || {};
    drawDonut(s.accounts || []);
    drawPlatformBars(s.accounts || []);
    drawAreaSparkline(buffers.busyRatio);
    drawBarSparkline(buffers.logRate);
    drawHeatStrip(buffers.conn);
  }

  // Donut: busy vs idle from the CURRENT snapshot (not buffered).
  function drawDonut(accounts) {
    const svg = nodes.svgDonut;
    clear(svg);
    const total = accounts.length;
    const busy = accounts.filter((a) => a.status === "busy").length;
    nodes.legendBusy.textContent = String(busy);
    nodes.legendIdle.textContent = String(total - busy);
    const cx = 60, cy = 60, r = 42, sw = 14, circ = 2 * Math.PI * r;

    // track (idle base)
    svg.appendChild(mk("circle", { cx, cy, r, fill: "none",
      stroke: "var(--surface-2)", "stroke-width": sw }));
    if (total === 0) {
      svg.appendChild(txt(cx, cy + 4, "0", "chart-emph", { "text-anchor": "middle", "font-size": "20" }));
      svg.appendChild(txt(cx, cy + 34, "no accounts", "chart-warming", { "text-anchor": "middle" }));
      return;
    }
    // busy arc
    const busyLen = (busy / total) * circ;
    const arc = mk("circle", { cx, cy, r, fill: "none", stroke: "var(--brand)",
      "stroke-width": sw, "stroke-linecap": "round",
      "stroke-dasharray": busyLen + " " + circ,
      transform: "rotate(-90 " + cx + " " + cy + ")" });
    svg.appendChild(arc);
    svg.appendChild(txt(cx, cy + 6, String(busy), "chart-emph", { "text-anchor": "middle", "font-size": "22" }));
  }

  // Per-platform STACKED bars (busy lime + idle surface-2), sorted desc.
  function drawPlatformBars(accounts) {
    const svg = nodes.svgPlatforms;
    clear(svg);
    if (!accounts.length) {
      svg.appendChild(txt(100, 60, "No accounts to distribute.", "chart-warming", { "text-anchor": "middle" }));
      return;
    }
    const byPlat = {};
    for (const a of accounts) {
      const p = a.platform || "—";
      if (!byPlat[p]) byPlat[p] = { total: 0, busy: 0 };
      byPlat[p].total += 1;
      if (a.status === "busy") byPlat[p].busy += 1;
    }
    const rows = Object.keys(byPlat).map((p) => ({ p, ...byPlat[p] }))
      .sort((a, b) => b.total - a.total);
    const W = 200, H = 120, labelW = 62, barMax = W - labelW - 22;
    const maxTotal = Math.max(1, ...rows.map((r) => r.total));
    const rowH = Math.min(22, (H - 8) / rows.length);
    rows.forEach((r, i) => {
      const y = 6 + i * rowH;
      const bh = Math.max(6, rowH - 6);
      const full = (r.total / maxTotal) * barMax;
      const busyW = (r.busy / r.total) * full;
      svg.appendChild(txt(2, y + bh - 1, platformLabel(r.p), "chart-warming",
        { "font-size": "9", fill: "var(--text-muted)" }));
      // idle track (full)
      svg.appendChild(mk("rect", { x: labelW, y, width: full, height: bh, rx: 3, fill: "var(--surface-2)" }));
      // busy portion
      if (busyW > 0) svg.appendChild(mk("rect", { x: labelW, y, width: busyW, height: bh, rx: 3, fill: "var(--brand)" }));
      svg.appendChild(txt(W - 2, y + bh - 1, String(r.total), "chart-emph",
        { "text-anchor": "end", "font-size": "9", fill: "var(--text)" }));
    });
  }

  // Activity area sparkline of busyRatio (0..1) over the session.
  function drawAreaSparkline(data) {
    const svg = nodes.svgActivity;
    clear(svg);
    const W = 300, H = 120;
    if (data.length < 2) {
      svg.appendChild(mk("line", { x1: 0, y1: H / 2, x2: W, y2: H / 2,
        stroke: "var(--border)", "stroke-width": 1, "stroke-dasharray": "4 4" }));
      svg.appendChild(txt(W / 2, H / 2 - 8, "Warming — collecting activity…", "chart-warming", { "text-anchor": "middle" }));
      nodes.activityReadout.textContent = "this session";
      return;
    }
    // grid lines at 25/50/75%
    [0.25, 0.5, 0.75].forEach((g) => {
      const y = H - g * H;
      svg.appendChild(mk("line", { x1: 0, y1: y, x2: W, y2: y, stroke: "var(--border)", "stroke-width": 0.5, opacity: 0.5 }));
    });
    const n = data.length;
    const px = (i) => (i / (n - 1)) * W;
    const py = (v) => H - v * H;
    let d = "M " + px(0) + " " + py(data[0]);
    for (let i = 1; i < n; i++) d += " L " + px(i) + " " + py(data[i]);
    // area fill (gradient defined per-draw)
    const gid = "act-grad";
    const defs = mk("defs", {});
    const grad = mk("linearGradient", { id: gid, x1: 0, y1: 0, x2: 0, y2: 1 });
    grad.appendChild(mk("stop", { offset: "0%", "stop-color": "var(--brand)", "stop-opacity": "0.28" }));
    grad.appendChild(mk("stop", { offset: "100%", "stop-color": "var(--brand)", "stop-opacity": "0" }));
    defs.appendChild(grad); svg.appendChild(defs);
    svg.appendChild(mk("path", { d: d + " L " + W + " " + H + " L 0 " + H + " Z", fill: "url(#" + gid + ")", stroke: "none" }));
    svg.appendChild(mk("path", { d, fill: "none", stroke: "var(--brand)", "stroke-width": 2, "stroke-linejoin": "round" }));
    // endpoint dot with steady glow
    const lastV = data[n - 1];
    svg.appendChild(mk("circle", { cx: W, cy: py(lastV), r: 4, fill: "var(--brand)",
      style: "filter:drop-shadow(0 0 4px var(--brand))" }));
    nodes.activityReadout.textContent = "now " + Math.round(lastV * 100) + "%";
  }

  // Log throughput bars: lines/tick, tallest emphasized.
  function drawBarSparkline(data) {
    const svg = nodes.svgThroughput;
    clear(svg);
    const W = 300, H = 120;
    if (!data.length || data.every((v) => v === 0)) {
      svg.appendChild(txt(W / 2, H / 2, "No log output yet this session.", "chart-warming", { "text-anchor": "middle" }));
      nodes.throughputReadout.textContent = "this session";
      return;
    }
    const n = data.length;
    const max = Math.max(1, ...data);
    const bw = W / n;
    const peak = max, avg = Math.round(data.reduce((a, b) => a + b, 0) / n);
    data.forEach((v, i) => {
      const h = (v / max) * (H - 6);
      const isPeak = v === max && v > 0;
      svg.appendChild(mk("rect", {
        x: i * bw + bw * 0.15, y: H - h, width: bw * 0.7, height: h, rx: 1.5,
        fill: "var(--info)", opacity: isPeak ? 1 : 0.55,
      }));
    });
    nodes.throughputReadout.textContent = "peak " + peak + " · avg " + avg;
  }

  // Connectivity heat-strip: 1=connected (green), 0=stale (danger-soft), newest right.
  function drawHeatStrip(data) {
    const svg = nodes.svgUptime;
    clear(svg);
    const W = 300, H = 22;
    if (!data.length) {
      svg.appendChild(mk("rect", { x: 0, y: 4, width: 10, height: H - 8, rx: 2, fill: "var(--success)" }));
      nodes.uptimeReadout.textContent = "monitoring…";
      return;
    }
    const n = data.length;
    const cw = W / n;
    let up = 0, stale = 0, longestGap = 0, curGap = 0;
    data.forEach((v, i) => {
      if (v) { up++; curGap = 0; } else { stale++; curGap++; if (curGap > longestGap) longestGap = curGap; }
      svg.appendChild(mk("rect", {
        x: i * cw + 0.5, y: 3, width: Math.max(1, cw - 1), height: H - 6, rx: 1,
        fill: v ? "var(--success)" : "var(--danger-soft)",
      }));
    });
    const pct = Math.round((up / n) * 100);
    nodes.uptimeReadout.textContent = pct + "% up · " + stale + " stale" + (longestGap > 1 ? " · gap " + longestGap : "");
  }

  // ==================================================================== 1s local ticker
  // Timers keep ticking independent of event arrival so they run during stale periods.
  setInterval(() => {
    const now = Date.now();

    // session uptime
    const up = state.sessionStartAt ? fmtDur(now - state.sessionStartAt) : "00:00:00";
    nodes.sessionUptime.textContent = up;
    nodes.sessionUptimeHero.textContent = up;
    nodes.ctUptime.textContent = up;

    // current run duration = now - firstSeenAt[currentJobId]
    let runTxt = "—";
    if (state.currentJobId && firstSeenAt.has(state.currentJobId)) {
      runTxt = fmtMs(now - firstSeenAt.get(state.currentJobId));
    }
    nodes.runDuration.textContent = runTxt;
    nodes.kpiRunDuration.textContent = runTxt === "—" ? "—" : runTxt;
    nodes.ctRun.textContent = runTxt;

    // jobs / session
    nodes.jobsCount.textContent = String(state.distinctJobs);
    nodes.heroJobs.textContent = String(state.distinctJobs);
    nodes.ctJobs.textContent = String(state.distinctJobs);
    nodes.ctRestarts.textContent = String(state.restartCount);

    // last poll age + color
    if (state.lastStatusAt) {
      const age = (now - state.lastStatusAt) / 1000;
      nodes.ctPoll.textContent = age.toFixed(1) + "s";
      nodes.ctPoll.className = "mono " + (age > STALE_MS / 1000 ? "ct-danger" : age > 2 ? "ct-warn" : "ct-ok");
    }

    // hero live-dot reflects real run state
    const live = !!state.currentJobId;
    nodes.heroDot.className = "live-dot" + (live ? " is-live" : "");

    // log live-dot goes quiet after silence
    if (now - state.lastLogAt > LIVE_DOT_SILENCE_MS) nodes.logDot.classList.remove("is-live");
  }, 1000);

  // ==================================================================== unified 1s sampler
  // ALL tick series (busyRatio, conn, logRate) advance on this ONE fixed 1s cadence so their
  // array indices share a common wall-clock x-axis — the activity, connectivity, and
  // throughput charts are drawn as parallel "this session" timelines. Sampling is decoupled
  // from the status-event cadence: each tick reads the LATEST snapshot instead of pushing per
  // event. When the snapshot is stale we push 0 (honest: no observed activity — never faked).
  setInterval(() => {
    const fresh = !!state.lastStatusAt && (Date.now() - state.lastStatusAt <= STALE_MS);
    const s = fresh ? state.lastStatus : null;
    const accounts = (s && s.accounts) || [];
    const total = accounts.length;
    const busy = accounts.filter((a) => a.status === "busy").length;
    pushCapped(buffers.conn, fresh ? 1 : 0);
    pushCapped(buffers.busyRatio, total ? busy / total : 0);
    pushCapped(buffers.logRate, state.logRateBucket);
    state.logRateBucket = 0;
    scheduleChartRender();
  }, 1000);

  // ==================================================================== staleness watchdog
  setInterval(() => {
    if (!state.lastStatusAt) { renderConnection("connecting"); return; }
    if (Date.now() - state.lastStatusAt > STALE_MS) {
      // The connectivity buffer is sampled by the 1s ticker above; here we only reflect the
      // stale condition into the badge + dim the KPIs (no buffer writes, no fabricated data).
      renderConnection("stale");
      dimKpisForStale(true);
    } else {
      dimKpisForStale(false);
    }
  }, 1500);

  function dimKpisForStale(dim) {
    document.querySelectorAll(".kpi-value").forEach((n) => n.classList.toggle("dim", dim));
  }

  // ==================================================================== preflight rendering
  //
  // The worker's launch preflight (aizu.worker.preflight) rides GET /status. This file
  // renders it and NOTHING else: it never decides that a check passed, never invents copy
  // for a check id, and never treats a missing report as healthy. Every string shown —
  // title, detail, remedy — comes from the report, so a new check appears here correctly
  // without a UI change, and every one of them is written with textContent, never markup
  // (the details are machine-authored strings that quote paths and exception types).

  const PF_STATUS_PILL = {
    pass: "pill pill-success", fail: "pill pill-danger",
    unknown: "pill pill-warn", skip: "pill pill-neutral",
  };

  function pfChecks(pf) {
    return pf && Array.isArray(pf.checks) ? pf.checks : [];
  }
  function pfCheck(pf, id) {
    return pfChecks(pf).find((c) => c && c.id === id) || null;
  }
  function pfFailing(pf) {
    // "Not settled" — fail or unknown. A skip is a deliberate non-answer, not a problem.
    return pfChecks(pf).filter((c) => c.status === "fail" || c.status === "unknown");
  }
  function pfBlockers(pf) {
    return pfChecks(pf).filter((c) => c.severity === "fatal" && c.status === "fail");
  }

  /// The shell's own "something is wrong with this box" line, on the dashboard.
  ///
  /// The sidecar's preflight cannot see any of it — a config.toml that would not parse, a
  /// Chrome this app could not start, a Chrome that came up as the wrong browser — so the
  /// health strip below, which renders only that report, was never going to show it. It
  /// lived in the wizard footer alone, behind a button most operators never press, under an
  /// if/else a running job won.
  function refreshShellNote() {
    invoke("get_shell_note")
      .then(renderShellNote)
      // A plain browser (no Tauri) or a backend not up yet: leave whatever is on screen
      // rather than blanking a warning because one poll failed.
      .catch(() => {});
  }

  function renderShellNote(note) {
    if (!nodes.shellStrip) return;
    const text = typeof note === "string" ? note.trim() : "";
    nodes.shellStrip.classList.toggle("hidden", !text);
    if (text) nodes.shellStripText.textContent = text;
  }

  // The dashboard's permanent health strip.
  function renderPreflightStrip(pf) {
    if (!nodes.pfStrip) return;
    const strip = nodes.pfStrip;
    strip.classList.remove("is-ok", "is-warn", "is-danger", "is-unknown");

    if (!pf) {
      strip.classList.add("is-unknown");
      nodes.pfStripPill.textContent = "checking…";
      nodes.pfStripPill.className = "pill pill-neutral";
      nodes.pfStripText.textContent =
        "Running the launch preflight — nothing has been cleared yet.";
      renderPreflightList(nodes.pfStripBody, null);
      return;
    }

    const blockers = pfBlockers(pf);
    const failing = pfFailing(pf);
    if (pf.blocking && blockers.length) {
      strip.classList.add("is-danger");
      nodes.pfStripPill.textContent = "parked";
      nodes.pfStripPill.className = "pill pill-danger";
      nodes.pfStripText.textContent =
        "This worker is NOT taking jobs — " + blockers[0].title +
        ". It re-checks every 30 seconds and resumes on its own once fixed.";
    } else if (failing.length) {
      strip.classList.add("is-warn");
      nodes.pfStripPill.textContent = failing.length + (failing.length === 1 ? " warning" : " warnings");
      nodes.pfStripPill.className = "pill pill-warn";
      nodes.pfStripText.textContent = failing.map((c) => c.title).join(" · ");
    } else {
      strip.classList.add("is-ok");
      nodes.pfStripPill.textContent = "ready";
      nodes.pfStripPill.className = "pill pill-success";
      nodes.pfStripText.textContent = pf.enforced
        ? "Every launch check passed on this box."
        : "Checks passed — enforcement is OFF (AIZU_PREFLIGHT_ENFORCE=0), so a failure " +
          "would not have parked this box.";
    }
    // Enforcement-off is loud on purpose: a fleet quietly running unenforced is exactly
    // the invisible state this feature exists to abolish.
    if (pf.enforced === false) strip.classList.add("is-warn");
    renderPreflightList(nodes.pfStripBody, pf);
  }

  // Full report as rows. Used by BOTH the strip's details drawer and the wizard's Verify
  // step, so the operator learns one screen and keeps it forever.
  function renderPreflightList(container, pf) {
    if (!container) return;
    container.textContent = "";
    if (!pf) {
      const p = document.createElement("div");
      p.className = "muted small";
      p.textContent = "The worker has not reported a preflight yet.";
      container.appendChild(p);
      return;
    }
    if (pf.enforced === false) {
      const warn = document.createElement("div");
      warn.className = "pf-enforce-off small";
      warn.textContent =
        "PREFLIGHT ENFORCEMENT IS OFF on this box (AIZU_PREFLIGHT_ENFORCE=0). Failing " +
        "checks are shown but will not stop it from taking jobs.";
      container.appendChild(warn);
    }
    for (const c of pfChecks(pf)) {
      container.appendChild(preflightRow(c));
    }
    if (typeof pf.ranAt === "number" && pf.ranAt > 0) {
      const foot = document.createElement("div");
      foot.className = "muted small mono pf-foot";
      foot.textContent = "checked " + new Date(pf.ranAt * 1000).toLocaleTimeString() +
        (typeof pf.durationMs === "number" ? " · " + pf.durationMs + "ms" : "");
      container.appendChild(foot);
    }
  }

  function preflightRow(c) {
    const row = document.createElement("div");
    row.className = "pf-row" + (c.status === "fail" ? " is-fail" : "");

    const badge = document.createElement("span");
    badge.className = PF_STATUS_PILL[c.status] || "pill pill-neutral";
    badge.textContent = c.status;
    row.appendChild(badge);

    const body = document.createElement("div");
    body.className = "pf-row-body";

    const title = document.createElement("div");
    title.className = "pf-row-title";
    title.textContent = c.title || c.id;
    if (c.severity === "fatal") {
      const sev = document.createElement("span");
      sev.className = "pf-sev";
      sev.textContent = "blocks jobs";
      title.appendChild(sev);
    }
    body.appendChild(title);

    if (c.detail) {
      const d = document.createElement("div");
      d.className = "pf-row-detail mono muted small";
      d.textContent = c.detail;
      body.appendChild(d);
    }
    // The remedy is the whole point: it is written by the check that failed, so it names
    // the exact variable/port/path and the exact action. Shown only when it matters.
    if (c.remedy && (c.status === "fail" || c.status === "unknown")) {
      const r = document.createElement("div");
      r.className = "pf-row-remedy small";
      r.textContent = c.remedy;
      body.appendChild(r);
    }
    row.appendChild(body);
    return row;
  }

  if (nodes.pfStripToggle) nodes.pfStripToggle.addEventListener("click", () => {
    const hidden = nodes.pfStripBody.classList.toggle("hidden");
    nodes.pfStripToggle.setAttribute("aria-expanded", String(!hidden));
  });
  wire("pf-strip-recheck", (node) =>
    invoke("run_preflight")
      .then(() => { nodes.pfStripText.textContent = "Re-checking…"; })
      .catch((e) => flashError(node, e)));
  wire("pf-strip-setup", () => openWizard());
  wire("shell-strip-setup", () => openWizard());

  // ==================================================================== setup wizard
  //
  // The step to show is DERIVED in Rust on every get_setup_state call from live config +
  // the live preflight (commands::derive_step) — this file keeps only a view cursor and
  // the operator's step-6 skips, neither of which is persisted anywhere. Consequences,
  // all free: quit mid-setup and reopen → land where the box actually is; fix the box by
  // hand outside the app → the wizard agrees with reality instead of re-asking.
  //
  // The skips are MIRRORED to Rust (`set_login_skips`) rather than kept here alone. They
  // used to live only in this file, where they could gate step 6 but never the derived
  // step — so a login row that is red and stays red (LinkedIn/X have unvalidated session
  // signatures) left Finish disabled forever with no way out of the wizard at all.

  const setup = {
    st: null,        // last SetupStateDto
    open: false,
    view: 0,         // which pane is displayed
    skipped: new Set(), // step-6 platforms the operator skipped (session-only, never on disk)
    inflight: false, // a get_setup_state is in flight (the poller must not stack them)
    busy: false,     // a mutating command is running — buttons disabled
  };

  const WZ_TITLES = [
    "Set up this worker PC", "Connect to your cloud", "Keys this PC needs",
    "Enrol this worker", "Platforms this PC runs", "The warmed Chrome",
    "Sign in to each platform", "Everything checks out",
  ];

  wire("btn-setup", () => openWizard());
  // B10 is terminal until a human acts, so its strip gets a direct route to the one step
  // that fixes it rather than dropping the operator at whatever step is derived.
  wire("reenrol-fix", () => openWizard(3));
  wire("wz-close", () => dismissWizard());
  wire("wz-later", () => dismissWizard());
  wire("wz-back", () => { setup.view = prevVisibleStep(setup.view); renderWizard(); });
  wire("wz-goto", () => {
    const n = parseInt(el("wz-goto").getAttribute("data-step"), 10);
    if (!isNaN(n)) { setup.view = clampStep(n); renderWizard(); }
  });
  wire("wz-next", (node) => onWizardNext(node));

  /// `at` forces a specific pane (used by the revoked-token strip); otherwise the step is
  /// whatever the backend derived from the live box.
  function openWizard(at) {
    setup.open = true;
    nodes.wzOverlay.classList.remove("hidden");
    // Land where the box actually is. A box that has never been configured starts at the
    // Welcome pane, which explains what to go and fetch before it asks for any of it.
    refreshSetup().then(() => {
      const st = setup.st;
      // Adopt whatever skips the backend is already deriving from (a webview reload must
      // not silently drop them and re-disable Finish).
      for (const p of (st && st.skippedLogins) || []) setup.skipped.add(p);
      const fresh = !st || (!st.dispatchBaseUrl && !st.setupComplete);
      setup.view = at != null ? clampStep(at) : (fresh ? 0 : clampStep(st && st.step));
      renderWizard();
    });
  }

  /// Dismissing an UNFINISHED wizard (✕ or "Close and fix later") records the deferral.
  ///
  /// `setup_complete` still stays false — the box remains registered, parked and visible to
  /// admins, every failing check keeps showing in the dashboard strip, and the rail's button
  /// keeps saying "Finish setup". The one thing it buys is that the wizard stops re-opening
  /// over the dashboard on every launch. Without it "later" meant "next launch, and the one
  /// after that, forever", which for anyone holding a red row they cannot clear from that PC
  /// was not an escape hatch but a locked door.
  function dismissWizard() {
    const st = setup.st;
    closeWizard();
    if (st && !st.setupComplete && !st.setupDeferred) {
      return invoke("defer_setup")
        .then((next) => { if (next && typeof next === "object") setup.st = next; })
        .then(() => renderSetupButton(setup.st))
        .catch((e) => console.warn("defer_setup failed:", e));
    }
    return null;
  }

  function closeWizard() {
    setup.open = false;
    nodes.wzOverlay.classList.add("hidden");
  }

  function clampStep(step) {
    const n = typeof step === "number" ? step : 1;
    return Math.max(0, Math.min(WZ_LAST_STEP, n));
  }

  async function refreshSetup() {
    if (setup.inflight) return;
    setup.inflight = true;
    try {
      setup.st = await invoke("get_setup_state");
      applyDispatchConfig(setup.st.dispatchBaseUrl);
      renderSetupButton(setup.st);
    } catch (e) {
      // Plain browser, or the backend is not up yet. Leave the last snapshot in place.
      console.warn("get_setup_state failed:", e);
    } finally {
      setup.inflight = false;
    }
    if (setup.open) renderWizard();
    return setup.st;
  }

  /// The rail's Setup button is the standing reminder for a box whose setup was DEFERRED.
  /// Deferring stops the wizard re-opening on launch, so this is the only thing left that
  /// says "this box is not finished" outside the preflight strip — it must not be subtle.
  function renderSetupButton(st) {
    if (!nodes.btnSetup) return;
    const unfinished = !!st && !st.setupComplete;
    nodes.btnSetup.classList.toggle("needs-setup", unfinished);
    if (nodes.btnSetupLabel) nodes.btnSetupLabel.textContent = unfinished ? "Finish setup" : "Setup";
    nodes.btnSetup.title = unfinished
      ? "Setup is not finished on this PC — open the checklist to see what is still red."
      : "Open the setup checklist for this worker PC";
  }

  /// Steps 5 (Chrome) and 6 (Sign in) do not exist on a box that advertises no browser
  /// platform. The list is derived from the selection, exactly as the report derives its
  /// `skip` rows — nothing here is a hardcoded sequence.
  function visibleSteps() {
    const st = setup.st;
    const hasCdp = !!(st && st.cdpPlatforms && st.cdpPlatforms.length);
    return hasCdp ? [0, 1, 2, 3, 4, 5, 6, 7] : [0, 1, 2, 3, 4, 7];
  }
  function nextVisibleStep(from) {
    const steps = visibleSteps();
    const i = steps.indexOf(from);
    return i >= 0 && i + 1 < steps.length ? steps[i + 1] : from;
  }
  function prevVisibleStep(from) {
    const steps = visibleSteps();
    const i = steps.indexOf(from);
    return i > 0 ? steps[i - 1] : 0;
  }

  /// Is the CURRENT pane's gate green? Welcome has no gate. Step 6 additionally accepts
  /// rows the operator explicitly skipped — that escape hatch exists because the LinkedIn
  /// and X session signatures are unvalidated and a false red must never trap anyone.
  ///
  /// On the LAST pane there is no such local clause: Finish needs `st.step` to have reached
  /// 8, which is why the skips are mirrored to the backend (`pushLoginSkips`). A skip that
  /// only lived here moved this function and left the Finish button dead.
  function canAdvance() {
    const st = setup.st;
    if (!st) return false;
    if (setup.view === 0) return true;
    if (st.step > setup.view) return true;
    if (setup.view === 6) return loginRows().every((r) => r.done);
    return false;
  }

  /// Mirror the operator's skips to the backend, which is where the step is derived and
  /// therefore the only place that can open the Finish gate. Fire-and-render: the pill
  /// flips immediately, the gate follows on the answer.
  function pushLoginSkips(node) {
    return invoke("set_login_skips", { platforms: Array.from(setup.skipped) })
      .then((state) => { if (state && typeof state === "object") setup.st = state; })
      .catch((e) => {
        // Say it out loud. A silent failure here looks exactly like the bug this replaced:
        // a "skipped" pill next to a Finish button that never enables.
        nodes.wzMsg.textContent = "Could not record that skip: " + errText(e);
        nodes.wzMsg.className = "wz-foot-msg small is-warn";
        if (node) flashError(node, e);
      })
      .then(() => { if (setup.open) renderWizard(); });
  }

  /// One row per advertised browser platform, with its live login state and whether the
  /// operator skipped it.
  function loginRows() {
    const st = setup.st;
    if (!st) return [];
    return (st.cdpPlatforms || []).map((p) => {
      const check = pfCheck(st.preflight, "login." + p);
      const status = check ? check.status : null;
      return {
        platform: p,
        status: status,
        check: check,
        skipped: setup.skipped.has(p),
        done: status === "pass" || status === "skip" || setup.skipped.has(p),
      };
    });
  }

  function renderWizard() {
    const st = setup.st;
    if (!st) return;
    const steps = visibleSteps();
    if (steps.indexOf(setup.view) < 0) setup.view = steps[0];

    el("wz-title").textContent = WZ_TITLES[setup.view] || WZ_TITLES[0];
    for (const pane of nodes.wzBody.querySelectorAll(".wz-pane")) {
      pane.classList.toggle("hidden", Number(pane.getAttribute("data-step")) !== setup.view);
    }
    renderStepRail(steps, st);

    // Per-pane content.
    if (setup.view === 1) renderStepConnect(st);
    if (setup.view === 2) renderStepKeys(st);
    if (setup.view === 3) renderStepEnrol(st);
    if (setup.view === 4) renderStepPlatforms(st);
    if (setup.view === 5) renderStepChrome(st);
    if (setup.view === 6) renderStepSignIn(st);
    if (setup.view === 7) renderPreflightList(el("wz-preflight"), st.preflight);

    // Footer.
    const last = setup.view === WZ_LAST_STEP;
    nodes.wzNext.textContent = last ? "Finish" : "Next";
    const ready = canAdvance() && !setup.busy;
    nodes.wzNext.disabled = !ready;
    nodes.wzNext.setAttribute("aria-disabled", String(!ready));
    nodes.wzBack.disabled = setup.view === 0 || setup.busy;

    // A step BEHIND this one went red (Chrome quit, a token was revoked, a key was
    // deleted). Offer the jump rather than performing it: a check that flaps would
    // otherwise bounce the operator out of whatever they are reading every 1.5 seconds.
    const goto = el("wz-goto");
    // Not during the restart window a save causes: the worker is briefly down, so every
    // gate above Connect reads red for a second and the offer would be pure noise.
    const regressed = st.step < setup.view && st.sidecarRunning && !setup.busy;
    goto.classList.toggle("hidden", !regressed);
    if (regressed) {
      goto.textContent = "Go to step " + st.step;
      goto.setAttribute("data-step", String(st.step));
    }
    // A live job makes every step that restarts the worker refuse; say so once, up front,
    // instead of letting the operator discover it by clicking Save.
    //
    // The startup problem is ADDED to that, never outranked by it. It used to lose the
    // if/else and therefore vanish for the whole length of a job — and the wizard footer was
    // the only place in the app that rendered it at all, so "a job is running" silently
    // deleted the one sentence explaining why Chrome is broken. It is also on the dashboard
    // now (#shell-strip), which is what main.rs and commands.rs have always claimed.
    if (st.jobActive || st.startupError) {
      const parts = [];
      if (st.startupError) parts.push("Startup problem: " + st.startupError);
      if (st.jobActive) {
        parts.push(
          "A job is running on this box — saving anything here would restart the worker " +
          "and kill that run. Wait for it, or stop it from the dashboard.");
      }
      nodes.wzMsg.textContent = parts.join(" — ");
      nodes.wzMsg.className =
        "wz-foot-msg small " + (st.startupError ? "is-danger" : "is-warn");
    } else if (last && !ready && !setup.busy) {
      // A disabled Finish with no explanation is how this wizard became a trap. Name what
      // is holding it AND the way out, on the pane where the operator is stuck.
      nodes.wzMsg.textContent =
        "Finish is waiting on a check that is still red. If it cannot be fixed from this PC — " +
        "or you believe the check itself is wrong — use “Close and fix later”: the box " +
        "keeps running and your admins keep seeing the warning.";
      nodes.wzMsg.className = "wz-foot-msg small is-warn";
    } else if (!setup.busy) {
      nodes.wzMsg.textContent = "";
      nodes.wzMsg.className = "wz-foot-msg small";
    }
  }

  function renderStepRail(steps, st) {
    nodes.wzSteps.textContent = "";
    steps.forEach((n) => {
      const li = document.createElement("li");
      li.className = "wz-step" +
        (n === setup.view ? " is-current" : "") +
        (st.step > n ? " is-done" : "");
      const dot = document.createElement("span");
      dot.className = "wz-step-dot";
      dot.textContent = st.step > n ? "✓" : String(n);
      li.appendChild(dot);
      const label = document.createElement("span");
      label.className = "wz-step-label";
      label.textContent = ["Start", "Connect", "Keys", "Enrol", "Platforms", "Chrome", "Sign in", "Verify"][n];
      li.appendChild(label);
      // Backwards navigation only: you may revisit a step the box has already passed,
      // never jump ahead of a gate.
      if (n <= st.step) {
        li.classList.add("is-clickable");
        li.addEventListener("click", () => { setup.view = n; renderWizard(); });
      }
      nodes.wzSteps.appendChild(li);
    });
  }

  /// Paint one of the static `.wz-check` blocks from a preflight check id.
  function paintCheckBlock(blockId, checkId, fallbackDetail) {
    const block = el(blockId);
    if (!block) return null;
    const badge = block.querySelector("[data-badge]");
    const detail = block.querySelector("[data-detail]");
    const c = pfCheck(setup.st && setup.st.preflight, checkId);
    if (!c) {
      badge.className = "pill pill-neutral";
      badge.textContent = "checking…";
      if (detail && fallbackDetail !== undefined) detail.textContent = fallbackDetail;
      return null;
    }
    badge.className = PF_STATUS_PILL[c.status] || "pill pill-neutral";
    badge.textContent = c.status === "pass" ? "ready" : c.status;
    if (detail) {
      // Prefer the remedy when something is wrong — it is the sentence that tells the
      // operator what to DO. Fall back to the raw detail otherwise.
      detail.textContent = (c.status === "fail" || c.status === "unknown")
        ? (c.remedy || c.detail || "")
        : (c.detail || "");
    }
    return c;
  }

  /// Paint a `.wz-check` block from a plain boolean (the two gates that are NOT preflight
  /// checks: "the control surface answered" and "the cloud gave us an id").
  function paintBoolBlock(blockId, ok, okText, waitText) {
    const block = el(blockId);
    if (!block) return;
    const badge = block.querySelector("[data-badge]");
    const detail = block.querySelector("[data-detail]");
    badge.className = ok ? "pill pill-success" : "pill pill-warn";
    badge.textContent = ok ? "ready" : "waiting";
    if (detail) detail.textContent = ok ? okText : waitText;
  }

  // ---- step 1 · Connect
  function renderStepConnect(st) {
    const input = el("wz-dispatch");
    if (document.activeElement !== input) input.value = st.dispatchBaseUrl || "";
    paintBoolBlock(
      "wz-check-sidecar", st.sidecarRunning,
      "The worker is running on this PC and this app is talking to it.",
      "The worker has not answered yet. Save a dispatch URL to start it."
    );
  }
  wire("wz-test-dispatch", (node) => {
    const url = el("wz-dispatch").value.trim();
    const out = el("wz-dispatch-probe");
    out.textContent = "testing…";
    out.className = "small muted";
    return invoke("probe_dispatch", { url })
      .then((r) => {
        out.textContent = r.reachable
          ? "We reached your cloud (" + r.detail + ")."
          : "We could not reach it (" + r.detail + ") — you can still continue.";
        out.className = "small " + (r.reachable ? "is-ok" : "is-warn");
      })
      .catch((e) => { out.textContent = errText(e); out.className = "small is-danger"; flashError(node, e); });
  });

  // ---- step 2 · Keys
  function renderStepKeys(st) {
    const names = st.secretNames || [];
    el("wz-secret-state").textContent = names.indexOf("AIZU_SECRET_KEY") >= 0 ? "· stored" : "· not set";
    el("wz-openrouter-state").textContent =
      names.indexOf("OPENROUTER_API_KEY") >= 0 ? "· stored" : "· not set";
    paintCheckBlock("wz-check-token-persistence", "token_persistence");
    paintCheckBlock("wz-check-llm-backend", "llm_backend");
  }
  wire("wz-generate-key", (node) =>
    invoke("generate_secret_key")
      .then((key) => {
        const input = el("wz-secret-key");
        input.value = key;
        // Reveal it once: the operator may want to keep a copy, and it has not been saved
        // anywhere yet. It goes back to a password field as soon as Save runs.
        input.type = "text";
        el("wz-keys-msg").textContent = "Generated — click Save keys to store it on this PC.";
      })
      .catch((e) => flashError(node, e)));
  wire("wz-save-keys", (node) => {
    const entries = [];
    const secret = el("wz-secret-key").value.trim();
    const openrouter = el("wz-openrouter").value.trim();
    if (secret) entries.push({ key: "AIZU_SECRET_KEY", value: secret });
    if (openrouter) entries.push({ key: "OPENROUTER_API_KEY", value: openrouter });
    if (!entries.length) {
      el("wz-keys-msg").textContent = "Nothing to save — paste or generate a key first.";
      return null;
    }
    return runSetupCommand("save_worker_secrets", { entries }, "wz-keys-msg",
      "Saved — the worker is restarting so it can read them.", node)
      .then(() => {
        // Values are write-only from here on: the inputs are cleared and the backend only
        // ever tells us the NAMES that are stored.
        el("wz-secret-key").value = "";
        el("wz-secret-key").type = "password";
        el("wz-openrouter").value = "";
      });
  });

  // ---- step 3 · Enrol
  function renderStepEnrol(st) {
    el("wz-token-state").textContent = st.hasDispatchToken ? "· stored" : "· none";
    if (st.reenrolmentRequired) {
      paintBoolBlock("wz-check-enrolled", false, "",
        "This box's token was REJECTED by dispatch and cleared. Mint a NEW enrolment " +
        "token in the panel — this one cannot be reused.");
    } else {
      paintBoolBlock("wz-check-enrolled", !!st.workerId,
        "Enrolled as " + (st.workerId || ""),
        "Waiting for the cloud to accept this box. This takes a few seconds after saving.");
    }
  }
  wire("wz-save-token", (node) => {
    const token = el("wz-token").value.trim();
    if (!token) {
      el("wz-token-msg").textContent = "Paste the token first.";
      return null;
    }
    return runSetupCommand("save_enrolment_token", { token }, "wz-token-msg",
      "Saved — enrolling…", node)
      .then(() => { el("wz-token").value = ""; });
  });

  // ---- step 4 · Platforms
  function renderStepPlatforms(st) {
    const host = el("wz-platforms");
    // Rebuild only when the platform SET changed, so a click does not fight a 1.5s poll.
    const signature = (st.supportedPlatforms || []).join(",");
    if (host.getAttribute("data-signature") !== signature) {
      host.textContent = "";
      host.setAttribute("data-signature", signature);
      for (const p of st.supportedPlatforms || []) {
        const label = document.createElement("label");
        label.className = "wz-platform";
        const box = document.createElement("input");
        box.type = "checkbox";
        box.value = p;
        box.checked = (st.workerPlatforms || []).indexOf(p) >= 0;
        label.appendChild(box);
        const name = document.createElement("span");
        name.textContent = platformLabel(p);
        label.appendChild(name);
        if ((st.cdpPlatformNames || []).indexOf(p) >= 0) {
          const tag = document.createElement("span");
          tag.className = "wz-platform-tag";
          tag.textContent = "browser";
          label.appendChild(tag);
        }
        host.appendChild(label);
      }
    }
    paintCheckBlock("wz-check-capabilities", "capabilities");
  }
  wire("wz-save-platforms", (node) => {
    const chosen = Array.prototype.slice
      .call(el("wz-platforms").querySelectorAll("input[type=checkbox]"))
      .filter((b) => b.checked)
      .map((b) => b.value);
    if (!chosen.length) {
      el("wz-platforms-msg").textContent =
        "Pick at least one. A box that advertises nothing is never sent work.";
      return null;
    }
    return runSetupCommand("save_setup_step", { patch: { workerPlatforms: chosen } },
      "wz-platforms-msg", "Saved — the worker is restarting.", node);
  });

  // ---- step 5 · Chrome
  function renderStepChrome(st) {
    paintCheckBlock("wz-check-cdp-reachable", "cdp_reachable");
    paintCheckBlock("wz-check-cdp-attachable", "cdp_attachable");
    renderChromeProfile(st.chromeProfile);

    // Port drift (F10). The remedy string the check writes contains the exact port to
    // pin — `Setup → Chrome → "Use 9333"` — so the one-click repair reads it from there
    // rather than re-deriving a port the UI does not own.
    const drift = pfCheck(st.preflight, "cdp_port_drift");
    const banner = el("wz-drift-banner");
    const bad = drift && drift.status === "fail";
    banner.classList.toggle("hidden", !bad);
    if (bad) {
      el("wz-drift-text").textContent = drift.detail || drift.remedy || "";
      const port = portFromRemedy(drift.remedy);
      const fix = el("wz-drift-fix");
      fix.textContent = port ? "Use " + port : "Use it";
      fix.disabled = !port;
      fix.setAttribute("data-port", port ? String(port) : "");
    }
  }
  /// Pull the port out of the check's own remedy sentence (…Setup → Chrome → "Use 9333"…).
  function portFromRemedy(remedy) {
    const m = /"Use (\d{2,5})"/.exec(String(remedy || ""));
    if (!m) return null;
    const n = parseInt(m[1], 10);
    return n >= 1 && n <= 65535 ? n : null;
  }
  wire("wz-launch-chrome", (node) => {
    const out = el("wz-chrome-msg");
    out.textContent = "starting Chrome…";
    out.className = "small muted";
    setBusy(true);
    return invoke("launch_chrome")
      .then((url) => {
        out.textContent = "Chrome is up at " + url + ". Verifying the attach…";
        out.className = "small is-ok";
        return invoke("run_preflight");
      })
      .catch((e) => {
        // THE fix for the silent first run: ensure_running's real error used to exist only
        // as an eprintln! on a machine with no terminal attached.
        out.textContent = errText(e);
        out.className = "small is-danger";
        flashError(node, e);
      })
      .then(() => setBusy(false));
  });
  /// Where each browser's logins live, plus the one thing left to say about a profile
  /// from before the per-browser split.
  ///
  /// There is no question here and no button. Three rounds asked the operator which browser
  /// warmed a profile — a marker file, a decision table, a refusal, a declaration — and the
  /// last of them shipped an answer no launch site ever read, so the button was a dead end.
  /// The directory is now derived from the browser, so nobody can answer wrong and nothing
  /// has to be answered at all.
  function renderChromeProfile(profile) {
    const dirs = el("wz-profile-dirs");
    if (dirs) {
      dirs.textContent = profile
        ? "Chrome for Testing · " + profile.chromeForTestingDir +
          "\nGoogle Chrome · " + profile.chromeDir
        : "";
    }
    const block = el("wz-legacy-block");
    if (!block) return;
    // Two independent things can be sitting unused on disk: a pre-redesign profile in the
    // CURRENT base, and a profile at the shell's FORMER default location (from before the
    // base was unified). A box can have either, both, or neither — a box that has the second
    // and is never told just looks signed-out with no cause attached, which is the exact
    // silence this whole arc exists to end. Joined rather than ranked: neither is a control,
    // neither blocks, and dropping one to show the other would hide real logins.
    const notice = [
      profile && profile.legacyNotice,
      profile && profile.formerDefaultNotice,
    ].filter(Boolean).join("\n\n");
    // Never blocking, never a control: it says what is there and stops. It also must not
    // hide the step's own Launch button behind it — a legacy profile does not stop anything.
    block.classList.toggle("hidden", !notice);
    const text = el("wz-legacy-text");
    if (text) text.textContent = notice;
  }

  wire("wz-drift-fix", (node) => {
    const port = parseInt(el("wz-drift-fix").getAttribute("data-port"), 10);
    if (!port) return null;
    return runSetupCommand("save_setup_step", { patch: { cdpPort: port } },
      "wz-chrome-msg", "Pinned port " + port + " — the worker is restarting.", node);
  });

  // ---- step 5 · the browser binary
  //
  // The installer is ~30 MB because it carries NO browser, and a packaged worker cannot
  // borrow the one a developer's checkout downloaded — so on a fresh PC Chrome for Testing
  // has to be fetched once, here, before "Launch warmed Chrome" has anything to start.
  //
  // Nothing about this block is derived from get_setup_state: the report has no check that
  // says whether the binary is on disk, so this never claims to know. The ONLY state kept
  // here is "a download is in flight" — which is what disables the button, keeps the live
  // pane open, and stops a second click stacking a second 356 MB fetch on the same
  // directory. The download itself runs in Rust and can take MINUTES; deliberately it does
  // NOT go through setBusy(), because freezing Back/Next for a quarter of an hour would
  // trap the operator in this pane for the whole download.
  const chromeInstall = { running: false };

  wire("wz-install-chrome", (node) => {
    if (chromeInstall.running) return null;
    setChromeInstallRunning(true);
    setInstallMsg("Downloading — this can take several minutes. You can keep using the " +
                  "rest of setup; leave the app open.", "muted");
    return invoke("install_chrome_browser")
      .then((path) => {
        appendInstallLines(["done · " + path]);
        setInstallMsg("Done — this PC has its own Chrome at " + path, "is-ok");
        // Nothing in today's report watches the binary, but re-deriving costs one call and
        // keeps the step rail honest the day something does.
        return refreshSetup();
      })
      .catch((e) => {
        // Whatever the installer wrote as its last word, verbatim and on screen. This is
        // the entire diagnosis available to someone who will never see a terminal.
        appendInstallLines(["failed · " + errText(e)]);
        setInstallMsg("Download failed: " + errText(e) + " — you can try again.", "is-danger");
        flashError(node, e);
      })
      .then(() => setChromeInstallRunning(false));
  });

  /// The button owns its own disabled state rather than reading one off the wizard poll:
  /// renderStepChrome runs every 1.5s and must never repaint over a download in flight.
  function setChromeInstallRunning(on) {
    chromeInstall.running = on;
    const btn = el("wz-install-chrome");
    if (btn) {
      btn.disabled = on;
      btn.setAttribute("aria-disabled", String(on));
      btn.textContent = on ? "Downloading…" : "Download browser · 356 MB";
    }
    // Launch is disabled for the SAME window. It sits inches below on this pane, and
    // pressing it mid-download either resolves nothing (so system Chrome takes port 9333
    // and poisons it for the browser now arriving) or resolves a path inside a
    // half-extracted tree and launches a browser that never opens its CDP port. Both end
    // as "Chrome launched but did not become CDP-attachable", which reads as the download
    // having failed. Only this step's own two buttons are touched — the rest of setup
    // genuinely does stay usable, as the copy says.
    const launch = el("wz-launch-chrome");
    if (launch) {
      launch.disabled = on;
      launch.setAttribute("aria-disabled", String(on));
      launch.title = on ? "Available once the browser download finishes" : "";
    }
    const pane = el("wz-install-progress");
    if (pane && on) {
      pane.textContent = "";
      pane.classList.remove("hidden");
    }
  }

  function setInstallMsg(text, cls) {
    const msg = el("wz-install-msg");
    if (!msg) return;
    msg.textContent = text;
    msg.className = "small " + (cls || "muted");
  }

  /// One "chrome-install-progress" event per stderr line. Capped exactly like the log
  /// console: Playwright redraws its progress bar constantly, so a slow link arrives as
  /// hundreds of near-identical lines.
  function appendInstallLines(lines) {
    const pane = el("wz-install-progress");
    if (!pane) return;
    pane.classList.remove("hidden");
    let text = pane.textContent;
    for (const line of lines) text += line + "\n";
    const arr = text.split("\n");
    if (arr.length > MAX_INSTALL_LINES) text = arr.slice(arr.length - MAX_INSTALL_LINES).join("\n");
    pane.textContent = text;
    pane.scrollTop = pane.scrollHeight;
  }

  function onChromeInstallProgress(payload) {
    // A straggler emitted between the last write and the command settling has nowhere
    // meaningful to go — the terminal state is already on screen.
    if (!chromeInstall.running) return;
    const line = payload && payload.line;
    if (typeof line !== "string" || !line) return;
    appendInstallLines([line]);
  }

  // ---- step 6 · Sign in
  function renderStepSignIn(st) {
    const host = el("wz-logins");
    const rows = loginRows();
    const signature = rows.map((r) => r.platform).join(",");
    if (host.getAttribute("data-signature") !== signature) {
      host.textContent = "";
      host.setAttribute("data-signature", signature);
      for (const r of rows) {
        host.appendChild(buildLoginRow(r));
      }
    }
    // Repaint the live parts in place (the buttons keep their identity so a click mid-poll
    // is never lost).
    for (const r of rows) {
      const row = host.querySelector('[data-platform="' + r.platform + '"]');
      if (!row) continue;
      const badge = row.querySelector("[data-badge]");
      const detail = row.querySelector("[data-detail]");
      const skip = row.querySelector("[data-skip]");
      if (r.skipped) {
        badge.className = "pill pill-neutral";
        badge.textContent = "skipped";
      } else {
        badge.className = PF_STATUS_PILL[r.status] || "pill pill-neutral";
        badge.textContent = r.status === "pass" ? "signed in" : (r.status || "checking…");
      }
      detail.textContent = r.skipped
        ? "Skipped until you quit this app. Your admins still see the warning."
        : (r.check ? (r.check.status === "pass" ? (r.check.detail || "") : (r.check.remedy || r.check.detail || "")) : "");
      skip.textContent = r.skipped ? "Un-skip" : "Skip";
    }
  }
  function buildLoginRow(r) {
    const row = document.createElement("div");
    row.className = "wz-check wz-login";
    row.setAttribute("data-platform", r.platform);

    const badge = document.createElement("span");
    badge.className = "pill pill-neutral";
    badge.setAttribute("data-badge", "");
    badge.textContent = "checking…";
    row.appendChild(badge);

    const body = document.createElement("div");
    const title = document.createElement("div");
    title.className = "wz-check-title";
    title.textContent = platformLabel(r.platform);
    body.appendChild(title);
    const detail = document.createElement("div");
    detail.className = "wz-check-detail muted small";
    detail.setAttribute("data-detail", "");
    body.appendChild(detail);
    row.appendChild(body);

    const actions = document.createElement("div");
    actions.className = "wz-login-actions";
    const open = document.createElement("button");
    open.type = "button";
    open.className = "btn btn-small";
    open.textContent = "Open login tab";
    open.addEventListener("click", () => {
      detail.textContent = "Opening a tab in the warmed Chrome — sign in there, including 2FA.";
      invoke("open_login_tab", { platform: r.platform })
        .catch((e) => { detail.textContent = errText(e); flashError(open, e); });
    });
    actions.appendChild(open);
    const skip = document.createElement("button");
    skip.type = "button";
    skip.className = "btn btn-small btn-ghost";
    skip.setAttribute("data-skip", "");
    skip.textContent = "Skip";
    skip.addEventListener("click", () => {
      if (setup.skipped.has(r.platform)) setup.skipped.delete(r.platform);
      else setup.skipped.add(r.platform);
      renderWizard();
      // The skip has to reach the Rust derivation or it can move this pane and nothing
      // else — which is precisely how a permanently red login row made Finish unreachable.
      pushLoginSkips(skip);
    });
    actions.appendChild(skip);
    row.appendChild(actions);
    return row;
  }

  // ---- footer Next / Finish
  function onWizardNext(node) {
    if (setup.view === WZ_LAST_STEP) {
      // No "finish anyway": Finish is enabled only when the DERIVED step reached 8, i.e.
      // every gate this wizard walks is settled. That is NOT the same as "no fatal check is
      // failing" — the earlier gates include warn-severity rows (the sign-in ones), so a
      // red LinkedIn badge holds this button too until the operator skips it. The genuine
      // way out when a check is wrong and cannot be cleared is "Close and fix later", which
      // defers rather than completes.
      return runSetupCommand("finish_setup", {}, "wz-msg", "", node)
        .then(() => closeWizard());
    }
    // Step 1 saves the URL it is showing before advancing — the operator typed it here.
    if (setup.view === 1) {
      const url = el("wz-dispatch").value.trim();
      const st = setup.st || {};
      if (url && url !== st.dispatchBaseUrl) {
        return runSetupCommand("save_setup_step", { patch: { dispatchBaseUrl: url } },
          "wz-msg", "Saved — starting the worker…", node)
          .then(() => {
            // Advance only if the gate actually went green (the worker answered). If it
            // did not, the operator stays here looking at the row that says why.
            if (setup.st && setup.st.step > 1) {
              setup.view = nextVisibleStep(1);
              renderWizard();
            }
          });
      }
    }
    setup.view = nextVisibleStep(setup.view);
    renderWizard();
    return null;
  }

  /// Run a mutating setup command: disable the footer, invoke, report into `msgId`, then
  /// re-derive the whole state (which is what moves the step rail).
  function runSetupCommand(name, args, msgId, okMsg, node) {
    setBusy(true);
    const msg = el(msgId);
    if (msg) { msg.textContent = "working…"; msg.className = msg.className.replace(/ is-\w+/g, ""); }
    return invoke(name, args)
      .then((state) => {
        if (state && typeof state === "object") setup.st = state;
        if (msg && okMsg) { msg.textContent = okMsg; msg.className += " is-ok"; }
        else if (msg) msg.textContent = "";
      })
      .catch((e) => {
        if (msg) { msg.textContent = errText(e); msg.className += " is-danger"; }
        flashError(node, e);
      })
      .then(() => {
        setBusy(false);
        return refreshSetup();
      });
  }

  function setBusy(on) {
    setup.busy = on;
    if (setup.open) renderWizard();
  }

  // ==================================================================== helpers
  function pushCapped(arr, v) { arr.push(v); if (arr.length > BUFFER_MAX_TICKS) arr.shift(); }

  /// Tauri rejects with a plain string; a JS throw carries `.message`. Normalize both.
  function errText(e) {
    if (e == null) return "something went wrong";
    if (typeof e === "string") return e;
    return String(e.message || e);
  }

  function mk(tag, attrs) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const k in attrs) node.setAttribute(k, attrs[k]);
    return node;
  }
  function txt(x, y, content, cls, attrs) {
    const t = mk("text", Object.assign({ x, y }, attrs || {}));
    if (cls) t.setAttribute("class", cls);
    t.textContent = content;
    return t;
  }
  function clear(svg) { while (svg && svg.firstChild) svg.removeChild(svg.firstChild); }

  function clampCapacity(raw) {
    const n = parseInt(raw, 10);
    if (Number.isNaN(n)) return 1;
    return Math.max(0, Math.min(8, n));
  }
  function clampPort(raw) {
    const n = parseInt(raw, 10);
    if (Number.isNaN(n) || n < 1 || n > 65535) return 8788;
    return n;
  }

  // Mirror the panel's shared platformLabel (x → X, linkedin → LinkedIn, etc.).
  function platformLabel(p) {
    const map = {
      instagram: "Instagram", youtube: "YouTube", telegram: "Telegram", reddit: "Reddit",
      x: "X", linkedin: "LinkedIn", threads: "Threads", quora: "Quora",
    };
    return map[p] || p || "—";
  }
  function shortenPath(p) {
    if (!p) return "—";
    const parts = p.split(/[/\\]/);
    return parts.length > 2 ? "…/" + parts.slice(-2).join("/") : p;
  }
  function shortenUrl(u) {
    if (!u) return "";
    try { return new URL(u).host; } catch (e) { return u; }
  }
  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  // hh:mm:ss for session uptime
  function fmtDur(ms) {
    const sec = Math.max(0, Math.floor(ms / 1000));
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    return pad(h) + ":" + pad(m) + ":" + pad(s);
  }
  // mm:ss for run duration
  function fmtMs(ms) {
    const sec = Math.max(0, Math.floor(ms / 1000));
    const m = Math.floor(sec / 60), s = sec % 60;
    return pad(m) + ":" + pad(s);
  }
  function pad(n) { return (n < 10 ? "0" : "") + n; }

  // Step 0's "Where do I get a token?" — deliberately NOT a shell-open of a URL. Opening
  // an external browser needs a `shell:allow-open` capability this app does not currently
  // grant, and a button that silently does nothing is worse than one that tells you where
  // to look. It prints the exact path next to the button instead.
  wire("wz-open-panel", () => {
    const base = (setup.st && setup.st.dispatchBaseUrl) || "";
    const hint = el("wz-panel-hint");
    hint.textContent = base
      ? "Open " + base.replace(/\/+$/, "") + "/app/  →  Fleet  →  Add worker, on any computer."
      : "In the panel (Fleet → Add worker). Set the cloud address on the next step first.";
  });

  // -------------------------------------------------------------------- boot
  renderConnection("connecting");
  renderPreflightStrip(null);
  // Before the first status event: a shell problem is at its LOUDEST on a box whose sidecar
  // never came up, which is exactly the box that never emits one.
  refreshShellNote();
  scheduleChartRender();

  // Prime the setup state, then decide whether this is a first run. A box that has never
  // finished setup opens straight into the wizard — it used to open a dead dashboard whose
  // only configuration path was a 7-tap easter egg, with the real reason printed to a
  // stderr no GUI operator can read.
  //
  // ONCE the operator has dismissed it with "Close and fix later", though, it stops opening
  // itself: re-presenting an unexitable modal on every launch is not a reminder, it is a
  // wall. The reminder is the rail's "Finish setup" button plus the preflight strip, both
  // of which stay until setup really is complete.
  (async () => {
    const st = await refreshSetup();
    if (st && !st.setupComplete && !st.setupDeferred) openWizard();
  })();
})();
