/* ============================================================
   Chart Lab — shared sample dataset
   Same numbers feed BOTH the custom-SVG and Recharts pages so the
   two approaches are an apples-to-apples visual comparison.
   Classic script: exposes window.LabData.
   ============================================================ */
(function () {
  "use strict";

  /* Five series with data-viz colors that stay legible on the light
     canvas AND the dark canvas (indigo-led to echo Pulse's identity). */
  var CHANNELS = [
    { name: "Instagram", color: "#6d63ff" },
    { name: "Facebook",  color: "#38bdf8" },
    { name: "TikTok",    color: "#f472b6" },
    { name: "LinkedIn",  color: "#34d399" },
    { name: "X",         color: "#f59e0b" }
  ];

  var TICKS = ["Wk 1", "Wk 2", "Wk 3", "Wk 4", "Wk 5", "Wk 6",
               "Wk 7", "Wk 8", "Wk 9", "Wk 10", "Wk 11", "Wk 12"];

  /* leads per week, per channel */
  var SERIES = [
    [120, 138, 132, 158, 171, 165, 188, 204, 197, 226, 241, 268], // Instagram
    [ 88,  94, 110, 102, 121, 134, 128, 142, 155, 149, 163, 178], // Facebook
    [ 42,  58,  73,  69,  91, 110, 132, 121, 148, 167, 159, 184], // TikTok
    [ 64,  70,  66,  78,  82,  79,  88,  95,  91,  99, 104, 112], // LinkedIn
    [ 31,  29,  38,  44,  41,  52,  49,  58,  63,  60,  71,  77]  // X
  ];

  /* cost-per-lead trend over the same 12 weeks (area chart) */
  var CPL_TREND = [9.40, 9.10, 8.80, 8.95, 8.30, 7.90, 8.05, 7.40, 7.10, 6.80, 6.95, 6.40];

  /* spend by channel this period (donut) — index-aligned with CHANNELS */
  var SPEND = [4820, 3140, 2760, 1980, 1240];

  /* this period vs previous period leads, per channel (grouped bars) */
  var BARS = [
    { channel: "Instagram", current: 268, previous: 226 },
    { channel: "Facebook",  current: 178, previous: 149 },
    { channel: "TikTok",    current: 184, previous: 167 },
    { channel: "LinkedIn",  current: 112, previous:  99 },
    { channel: "X",         current:  77, previous:  60 }
  ];

  /* KPI strip with mini sparkline series */
  var KPIS = [
    { label: "Total leads",   value: 8194, prefix: "",  delta: "+18.4%", up: true,
      spark: [410, 452, 438, 489, 512, 498, 547, 583, 561, 624, 658, 712] },
    { label: "Cost per lead", value: 6.40, prefix: "$", dec: 2, delta: "−12.0%", up: true,
      spark: [9.4, 9.1, 8.8, 8.95, 8.3, 7.9, 8.05, 7.4, 7.1, 6.8, 6.95, 6.4] },
    { label: "Conversion",    value: 4.7,  suffix: "%", dec: 1, delta: "+0.6pt", up: true,
      spark: [3.8, 3.9, 4.0, 3.9, 4.1, 4.2, 4.3, 4.2, 4.4, 4.5, 4.6, 4.7] },
    { label: "Spend",         value: 13940, prefix: "$", delta: "+5.2%", up: false,
      spark: [980, 1040, 1010, 1120, 1180, 1150, 1240, 1290, 1260, 1320, 1380, 1410] }
  ];

  window.LabData = Object.freeze({
    CHANNELS: CHANNELS,
    TICKS: TICKS,
    SERIES: SERIES,
    CPL_TREND: CPL_TREND,
    SPEND: SPEND,
    BARS: BARS,
    KPIS: KPIS
  });
})();
