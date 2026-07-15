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
  const BUFFER_MAX_TICKS = 180;     // 3 min at the unified 1s sample cadence; FIFO
  const MAX_JOB_HISTORY = 500;      // cap distinct-jobId tracking on an always-on box
  const LIVE_DOT_SILENCE_MS = 3000; // log live-dot goes quiet after this
  const CHECKPOINT_REARM_MS = 20000;
  const SVG_NS = "http://www.w3.org/2000/svg";
  const LOCAL_DISPATCH = "http://127.0.0.1:8765";
  const TAPS_TO_REVEAL = 7;
  const TAP_WINDOW_MS = 3000;

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
    updateStrip: el("update-strip"),
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
    // dev menu
    overlay: el("dev-overlay"), devDispatch: el("dev-dispatch"), devPort: el("dev-port"),
    devToken: el("dev-token"), devTokenState: el("dev-token-state"), devMsg: el("dev-msg"),
    devCloudNote: el("dev-cloud-note"),
    brand: el("brand-tap"),
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

  // ==================================================================== hidden dev menu
  let tapCount = 0, tapTimer = null;
  if (nodes.brand) nodes.brand.addEventListener("click", () => {
    tapCount += 1;
    clearTimeout(tapTimer);
    tapTimer = setTimeout(() => { tapCount = 0; }, TAP_WINDOW_MS);
    if (tapCount >= TAPS_TO_REVEAL) { tapCount = 0; openDevMenu(); }
  });

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
    // save_config restarts the app on success (no resolved promise expected);
    // a validation error comes back to dev-msg.
    return invoke("save_config", args).catch((e) => {
      nodes.devMsg.textContent = String(e && e.message ? e.message : e);
      flashError(node, e);
    });
  });

  // Scrim click + Esc close the dev overlay.
  if (nodes.overlay) nodes.overlay.addEventListener("click", (e) => { if (e.target === nodes.overlay) closeDevMenu(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDevMenu(); });

  // Prime dispatch host / env chip from config on load (fail soft).
  (async () => {
    try {
      const cfg = await invoke("get_config");
      applyDispatchConfig(cfg && cfg.dispatchBaseUrl);
    } catch (e) { /* plain browser or no config yet */ }
  })();
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

  function renderWorkerTile(st) {
    if (!st) { nodes.workerWord.textContent = "connecting"; nodes.workerDot.className = "live-dot sm"; return; }
    if (st.state === "healthy") {
      nodes.workerWord.textContent = "healthy";
      nodes.workerDot.className = "live-dot sm is-success";
      nodes.workerSub.textContent = "process";
    } else if (st.state === "backing_off") {
      const sec = st.restartInSec != null ? " · " + st.restartInSec.toFixed(1) + "s" : "";
      nodes.workerWord.textContent = "restarting";
      nodes.workerDot.className = "live-dot sm is-warn";
      nodes.workerSub.textContent = "backing off" + sec;
    } else if (st.state === "crashed") {
      nodes.workerWord.textContent = "crashed";
      nodes.workerDot.className = "live-dot sm is-danger";
      nodes.workerSub.textContent = "restart to recover";
    }
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
    if (controls.halt) { cls = "pill pill-danger"; word = controls.haltReason || "halting"; }
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
      const reason = controls.halt ? "halted" : controls.paused ? "paused" : controls.drain ? "draining" : null;
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
  }

  // ==================================================================== spine (worst-condition wins)
  function setSpine(kind) {
    const s = state.lastStatus, c = (s && s.controls) || {};
    const sd = state.lastSidecar;
    let color = "var(--info)"; // connecting default
    if (sd && sd.state === "crashed") color = "var(--danger)";
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

  // ==================================================================== helpers
  function pushCapped(arr, v) { arr.push(v); if (arr.length > BUFFER_MAX_TICKS) arr.shift(); }

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

  // -------------------------------------------------------------------- boot
  renderConnection("connecting");
  scheduleChartRender();
})();
