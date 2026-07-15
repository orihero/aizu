/* ============================================================
   Admin · Pulse rebuild — Overview sample data (ReelRadar)
   Real domain semantics from the engine: reels seen, relevant,
   matches, cloud spend, call routing, discovery funnel.
   Same numbers feed BOTH the custom-SVG and Recharts pages.
   Classic script: exposes window.OverviewData.
   ============================================================ */
(function () {
  "use strict";

  /* per-session series, oldest -> newest (12 sessions) */
  var TICKS    = ["S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9", "S10", "S11", "S12"];
  var REELS    = [28, 34, 22, 41, 38, 30, 45, 36, 52, 44, 58, 49];
  var RELEVANT = [16, 19, 13, 24, 22, 17, 27, 21, 31, 26, 34, 29];
  var MATCHES  = [ 4,  6,  3,  7,  5,  4,  8,  6,  9,  7, 11,  8];
  var SPEND    = [0.06, 0.09, 0.04, 0.12, 0.10, 0.07, 0.14, 0.09, 0.18, 0.13, 0.22, 0.16];

  /* cumulative cloud spend (rising area) — final reconciles with KPI */
  var SPEND_CUM = (function () {
    var out = [], t = 0;
    for (var i = 0; i < SPEND.length; i++) { t += SPEND[i]; out.push(Math.round(t * 100) / 100); }
    return out;
  })();

  var SERIES_META = [
    { name: "Reels seen", color: "#6d63ff" },
    { name: "Relevant",   color: "#38bdf8" },
    { name: "Matches",    color: "#22c55e" }
  ];

  var KPIS = [
    { label: "Reels seen",  value: 477,  delta: "+14%", up: true,  spark: REELS,
      meta: "127 already-seen skipped" },
    { label: "Matches",     value: 78,   delta: "+9%",  up: true,  spark: MATCHES,
      meta: "188 comments scored" },
    { label: "Cloud spend", value: 1.40, prefix: "$", dec: 2, delta: "−6%", up: true, spark: SPEND,
      meta: "of $5.00 cap" },
    { label: "Sessions",    value: 12,   delta: "+2",   up: true,
      spark: [2, 3, 3, 4, 5, 5, 6, 7, 8, 9, 10, 12], meta: "1–2 / day pacing" }
  ];

  /* discovery funnel — reels -> relevant -> scored -> matches */
  var FUNNEL = [
    { name: "Reels seen", value: 477, color: "#6d63ff" },
    { name: "Relevant",   value: 279, color: "#38bdf8" },
    { name: "Scored",     value: 188, color: "#a78bfa" },
    { name: "Matches",    value:  78, color: "#22c55e" }
  ];

  /* LLM call routing — where the work ran */
  var ROUTING = [
    { name: "Local · text",   value: 920, color: "#6d63ff" },
    { name: "Local · vision", value: 446, color: "#38bdf8" },
    { name: "Cloud",          value:  78, color: "#c084fc" }
  ];

  window.OverviewData = Object.freeze({
    TICKS: TICKS,
    REELS: REELS, RELEVANT: RELEVANT, MATCHES: MATCHES,
    SPEND: SPEND, SPEND_CUM: SPEND_CUM,
    SERIES_META: SERIES_META,
    KPIS: KPIS,
    FUNNEL: FUNNEL,
    ROUTING: ROUTING
  });
})();
