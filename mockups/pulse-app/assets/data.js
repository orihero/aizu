/* ============================================================
   LeadFlow · Pulse — shared fake data (window.PulseData)
   Classic script (no modules — must work from file://).
   Frozen: page scripts read from here, never mutate shape.
   ============================================================ */
(function () {
  "use strict";

  var CHANNELS = [
    { key: "fb", name: "Facebook",  color: "#1877f2" },
    { key: "ig", name: "Instagram", color: "#e1306c" },
    { key: "li", name: "LinkedIn",  color: "#0a66c2" },
    { key: "tt", name: "TikTok",    color: "#16161a" },
    { key: "x",  name: "X",         color: "#6b7280" }
  ];

  var WORKSPACES = [
    { name: "Acme",         glyph: "A", meta: "12 campaigns · Pro",     accent: "lime"   },
    { name: "Northwind",    glyph: "N", meta: "7 campaigns · Growth",   accent: "indigo" },
    { name: "Voyager Labs", glyph: "V", meta: "4 campaigns · Starter",  accent: "ink"    }
  ];

  /* ---------------- DASHBOARD (ported verbatim from sample-3) ---------------- */
  var DASH_CAMPAIGNS = [
    { name: "Spring Launch Blitz", channel: 1, live: true  },
    { name: "B2B Pipeline Pro",    channel: 2, live: true  },
    { name: "Creator Collab Q2",   channel: 3, live: true  },
    { name: "Retarget & Convert",  channel: 0, live: false },
    { name: "Brand Pulse Weekly",  channel: 4, live: true  }
  ];

  var DASHBOARD = {
    today: {
      leads: 218, delta: "+6.4%", sub: "vs. yesterday · all channels combined",
      spark: [4, 7, 5, 9, 14, 12, 18, 24, 21, 28, 26, 31, 36, 34],
      goalPct: 34, goalCap: "<strong>218</strong> of 650 daily goal",
      cpl: 2.96, cplDelta: "−15%", cplBars: [4.3, 4.0, 3.7, 3.9, 3.4, 3.2, 3.1, 2.96],
      conv: 5.2, convDelta: "+0.6pt", active: 19,
      bars: [[64, 58], [61, 52], [38, 41], [36, 29], [19, 22]],
      funnel: [
        { name: "Impressions", val: "21.4K", w: 100 },
        { rate: "7.0% click-through" },
        { name: "Clicks", val: "1,496", w: 64 },
        { rate: "14.6% lead capture" },
        { name: "Leads", val: "218", w: 38 },
        { rate: "39.9% qualified" },
        { name: "Qualified", val: "87", w: 21 }
      ],
      camp: [[54, 2.40], [39, 4.62], [35, 1.70], [24, 3.32], [11, 3.95]],
      hour: "6–8 PM", hourSub: "highest lead capture today",
      heat: [.06,.04,.03,.03,.05,.10,.18,.30,.42,.48,.44,.40,.52,.46,.38,.42,.55,.72,.95,1,.88,.62,.34,.16]
    },
    week: {
      leads: 3084, delta: "+9.6%", sub: "vs. previous 7 days · all channels combined",
      spark: [355, 392, 348, 421, 458, 437, 489, 512, 478, 538, 561, 529, 588, 612],
      goalPct: 67, goalCap: "<strong>3,084</strong> of 4,600 weekly goal",
      cpl: 3.18, cplDelta: "−8%", cplBars: [3.9, 3.7, 3.8, 3.5, 3.4, 3.5, 3.3, 3.18],
      conv: 4.9, convDelta: "+0.2pt", active: 21,
      bars: [[980, 910], [856, 790], [512, 540], [488, 371], [248, 265]],
      funnel: [
        { name: "Impressions", val: "286K", w: 100 },
        { rate: "7.5% click-through" },
        { name: "Clicks", val: "21.4K", w: 66 },
        { rate: "14.4% lead capture" },
        { name: "Leads", val: "3,084", w: 39 },
        { rate: "42.0% qualified" },
        { name: "Qualified", val: "1,294", w: 22 }
      ],
      camp: [[768, 2.71], [512, 4.88], [463, 1.82], [342, 3.51], [151, 4.10]],
      hour: "7–9 PM", hourSub: "highest lead capture this week",
      heat: [.05,.04,.03,.04,.06,.11,.20,.33,.45,.50,.46,.41,.48,.44,.40,.46,.58,.70,.86,1,.97,.74,.40,.18]
    },
    month: {
      leads: 12847, delta: "+18.2%", sub: "vs. previous 30 days · all channels combined",
      spark: [320, 380, 365, 440, 505, 470, 560, 610, 585, 660, 720, 690, 790, 845],
      goalPct: 64, goalCap: "<strong>12,847</strong> of 20,000 monthly goal",
      cpl: 3.42, cplDelta: "−12%", cplBars: [5.1, 4.8, 4.6, 4.4, 4.1, 3.9, 3.7, 3.42],
      conv: 4.7, convDelta: "+0.4pt", active: 23,
      bars: [[4120, 3650], [3480, 2890], [2210, 2040], [1890, 1320], [1147, 1210]],
      funnel: [
        { name: "Impressions", val: "1.2M", w: 100 },
        { rate: "7.2% click-through" },
        { name: "Clicks", val: "86.4K", w: 68 },
        { rate: "14.9% lead capture" },
        { name: "Leads", val: "12,847", w: 41 },
        { rate: "40.5% qualified" },
        { name: "Qualified", val: "5,210", w: 23 }
      ],
      camp: [[3214, 2.84], [2108, 5.12], [1876, 1.94], [1422, 3.66], [640, 4.28]],
      hour: "6–8 PM", hourSub: "highest lead capture window",
      heat: [.07,.05,.04,.04,.06,.12,.22,.35,.47,.52,.48,.43,.55,.49,.42,.47,.60,.78,1,.96,.84,.58,.31,.14]
    }
  };

  /* ---------------- CAMPAIGNS PAGE ----------------
     status: live | paused | draft | ended            */
  var CAMPAIGNS = [
    { id: "c1",  name: "Spring Launch Blitz",  channels: [1, 0],    status: "live",   leads: 3214, cpl: 2.84, budget: 12000, spent: 9128,  spark: [38, 44, 41, 52, 58, 56, 64, 71], started: "Mar 4, 2026" },
    { id: "c2",  name: "B2B Pipeline Pro",     channels: [2],       status: "live",   leads: 2108, cpl: 5.12, budget: 15000, spent: 10793, spark: [22, 26, 31, 28, 34, 38, 41, 45], started: "Feb 18, 2026" },
    { id: "c3",  name: "Creator Collab Q2",    channels: [3, 1],    status: "live",   leads: 1876, cpl: 1.94, budget: 6000,  spent: 3640,  spark: [12, 18, 24, 31, 38, 47, 55, 62], started: "Apr 1, 2026" },
    { id: "c4",  name: "Retarget & Convert",   channels: [0],       status: "paused", leads: 1422, cpl: 3.66, budget: 8000,  spent: 5205,  spark: [44, 41, 38, 35, 30, 24, 18, 12], started: "Jan 12, 2026" },
    { id: "c5",  name: "Brand Pulse Weekly",   channels: [4],       status: "live",   leads: 640,  cpl: 4.28, budget: 4000,  spent: 2739,  spark: [8, 10, 9, 12, 14, 13, 16, 18],   started: "Mar 22, 2026" },
    { id: "c6",  name: "Summer Teaser Drop",   channels: [1, 3],    status: "draft",  leads: 0,    cpl: 0,    budget: 9000,  spent: 0,     spark: [0, 0, 0, 0, 0, 0, 0, 0],         started: "—" },
    { id: "c7",  name: "Webinar Wave 3",       channels: [2, 0],    status: "live",   leads: 894,  cpl: 6.05, budget: 11000, spent: 5409,  spark: [10, 14, 12, 18, 22, 26, 25, 30], started: "Apr 14, 2026" },
    { id: "c8",  name: "App Install Sprint",   channels: [3],       status: "paused", leads: 1102, cpl: 2.31, budget: 5000,  spent: 2546,  spark: [30, 34, 31, 28, 26, 22, 19, 15], started: "Feb 2, 2026" },
    { id: "c9",  name: "Holiday Early-Bird",   channels: [0, 1, 4], status: "draft",  leads: 0,    cpl: 0,    budget: 20000, spent: 0,     spark: [0, 0, 0, 0, 0, 0, 0, 0],         started: "—" },
    { id: "c10", name: "Q1 Newsletter Push",   channels: [2],       status: "ended",  leads: 2860, cpl: 3.09, budget: 9000,  spent: 8837,  spark: [52, 58, 61, 55, 48, 41, 33, 24], started: "Jan 2, 2026" },
    { id: "c11", name: "Lookalike Expansion",  channels: [0],       status: "live",   leads: 731,  cpl: 3.84, budget: 7000,  spent: 2807,  spark: [6, 9, 12, 16, 15, 19, 24, 28],   started: "May 6, 2026" },
    { id: "c12", name: "Video Views Booster",  channels: [3, 4],    status: "ended",  leads: 1540, cpl: 1.62, budget: 3000,  spent: 2495,  spark: [40, 46, 51, 49, 42, 35, 26, 16], started: "Dec 1, 2025" }
  ];

  /* ---------------- LEADS PAGE ----------------
     status: new | contacted | qualified | disqualified */
  var LEAD_FIRST = ["Maya", "Liam", "Sofia", "Noah", "Ava", "Ethan", "Isla", "Lucas", "Zoe", "Mateo", "Nora", "Felix", "Lena", "Oscar", "Ruby", "Hugo", "Mila", "Jonas", "Elif", "Dario", "Petra", "Yusuf", "Anya", "Marco", "Tara", "Ivan", "Cleo", "Sven", "Dana", "Rafael", "Greta", "Tom", "Aisha", "Niko", "Vera", "Otis"];
  var LEAD_LAST  = ["Kerr", "Novak", "Reyes", "Lindqvist", "Okafor", "Marsh", "Tanaka", "Petrov", "Alvarez", "Bauer", "Kimura", "Sorensen", "Dias", "Weber", "Costa", "Haddad", "Berg", "Moreau", "Silva", "Klein", "Varga", "Demir", "Fontaine", "Russo", "Nilsen", "Castillo", "Egede", "Horvat", "Lange", "Mbeki", "Ade", "Vance", "Iqbal", "Saar", "Toth", "Quinn"];
  var LEAD_STATUSES = ["new", "new", "new", "contacted", "contacted", "qualified", "qualified", "disqualified"];
  var LEAD_DATES = ["Jun 12", "Jun 12", "Jun 11", "Jun 11", "Jun 11", "Jun 10", "Jun 10", "Jun 9", "Jun 9", "Jun 8", "Jun 8", "Jun 7", "Jun 6", "Jun 5", "Jun 4", "Jun 3", "Jun 2", "Jun 1"];
  var ACTIVE_CAMPS = CAMPAIGNS.filter(function (c) { return c.status === "live" || c.status === "paused" || c.status === "ended"; });

  var LEADS = [];
  (function buildLeads() {
    for (var i = 0; i < 36; i++) {
      var first = LEAD_FIRST[i], last = LEAD_LAST[(i * 7 + 3) % LEAD_LAST.length];
      var camp = ACTIVE_CAMPS[(i * 5 + 2) % ACTIVE_CAMPS.length];
      LEADS.push({
        id: "L-" + (4820 - i * 7),
        name: first + " " + last,
        email: (first + "." + last).toLowerCase() + "@" + ["gmail.com", "outlook.com", "proton.me", "yahoo.com"][i % 4],
        campaign: camp.name,
        campaignId: camp.id,
        channel: camp.channels[i % camp.channels.length],
        status: LEAD_STATUSES[(i * 3 + 1) % LEAD_STATUSES.length],
        date: LEAD_DATES[Math.min(Math.floor(i / 2), LEAD_DATES.length - 1)],
        score: 96 - ((i * 11) % 62),
        phone: "+1 (555) 01" + String(20 + i).slice(-2) + "-" + String(1000 + i * 137).slice(-4),
        company: ["Brightline Co", "Vantage Labs", "Orchid & Pine", "Mintfield", "Coastal Forge", "Juniper Way", "Halcyon Group", "Fernworks", "Bluepeak", "Solstice IO"][i % 10],
        timeline: [
          { t: "Lead captured",   when: "via lead form" },
          { t: "Synced to CRM",   when: "2 min later" },
          { t: "Welcome email",   when: "1 hr later" }
        ]
      });
    }
  })();

  /* ---------------- REPORTS PAGE ----------------
     series: per-channel arrays (12 points per period)  */
  var REPORTS = {
    today: {
      label: "Today · hourly", ticks: ["8a", "9a", "10a", "11a", "12p", "1p", "2p", "3p", "4p", "5p", "6p", "7p"],
      series: [
        [4, 6, 7, 9, 8, 10, 12, 11, 13, 15, 18, 16],
        [3, 5, 6, 8, 9, 8, 10, 12, 14, 13, 16, 17],
        [2, 3, 4, 5, 6, 7, 6, 8, 7, 9, 8, 10],
        [1, 2, 3, 4, 4, 5, 6, 7, 8, 8, 10, 11],
        [1, 1, 2, 2, 3, 3, 4, 3, 4, 5, 5, 6]
      ],
      cplTrend: [3.4, 3.3, 3.2, 3.3, 3.1, 3.0, 3.1, 2.9, 3.0, 2.9, 2.95, 2.96],
      spend: [184, 162, 121, 88, 56],
      table: [
        ["Spring Launch Blitz", 1, 54, 2.40, "+12%"],
        ["B2B Pipeline Pro",    2, 39, 4.62, "+6%"],
        ["Creator Collab Q2",   3, 35, 1.70, "+21%"],
        ["Retarget & Convert",  0, 24, 3.32, "−4%"],
        ["Brand Pulse Weekly",  4, 11, 3.95, "+2%"]
      ]
    },
    week: {
      label: "This week · daily", ticks: ["Fri", "Sat", "Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun", "Mon", "Tue"],
      series: [
        [98, 112, 104, 128, 136, 131, 148, 152, 144, 160, 168, 159],
        [86, 94, 90, 108, 116, 112, 124, 131, 126, 138, 146, 141],
        [52, 56, 50, 62, 68, 64, 72, 76, 70, 80, 84, 78],
        [44, 50, 46, 58, 62, 60, 70, 74, 68, 78, 84, 80],
        [22, 24, 23, 28, 30, 28, 32, 34, 31, 36, 38, 35]
      ],
      cplTrend: [3.7, 3.6, 3.65, 3.5, 3.45, 3.5, 3.4, 3.35, 3.3, 3.25, 3.2, 3.18],
      spend: [3120, 2680, 1840, 1260, 880],
      table: [
        ["Spring Launch Blitz", 1, 768, 2.71, "+9%"],
        ["B2B Pipeline Pro",    2, 512, 4.88, "+4%"],
        ["Creator Collab Q2",   3, 463, 1.82, "+18%"],
        ["Retarget & Convert",  0, 342, 3.51, "−6%"],
        ["Brand Pulse Weekly",  4, 151, 4.10, "+3%"]
      ]
    },
    month: {
      label: "30 days · ~3-day buckets", ticks: ["May 14", "17", "20", "23", "26", "29", "Jun 1", "4", "7", "10", "11", "12"],
      series: [
        [310, 348, 332, 392, 421, 405, 462, 488, 471, 520, 548, 533],
        [262, 290, 281, 330, 356, 344, 388, 412, 398, 442, 466, 451],
        [168, 184, 176, 204, 220, 212, 238, 252, 244, 268, 282, 272],
        [142, 160, 152, 182, 198, 190, 218, 234, 224, 252, 268, 258],
        [82, 90, 86, 100, 108, 104, 116, 124, 118, 132, 140, 134]
      ],
      cplTrend: [4.1, 4.0, 3.95, 3.85, 3.8, 3.7, 3.65, 3.6, 3.55, 3.5, 3.45, 3.42],
      spend: [13980, 11820, 8140, 5530, 3890],
      table: [
        ["Spring Launch Blitz", 1, 3214, 2.84, "+14%"],
        ["B2B Pipeline Pro",    2, 2108, 5.12, "+7%"],
        ["Creator Collab Q2",   3, 1876, 1.94, "+24%"],
        ["Retarget & Convert",  0, 1422, 3.66, "−8%"],
        ["Webinar Wave 3",      2, 894,  6.05, "+11%"],
        ["Brand Pulse Weekly",  4, 640,  4.28, "+5%"]
      ]
    }
  };

  /* ---------------- SETTINGS PAGE ---------------- */
  var TEAM = [
    { name: "Jordan Mercer", email: "jordan@acme.co",  role: "Owner",  initials: "JM", you: true  },
    { name: "Priya Shah",    email: "priya@acme.co",   role: "Admin",  initials: "PS", you: false },
    { name: "Tom Eriksen",   email: "tom@acme.co",     role: "Member", initials: "TE", you: false },
    { name: "Lucia Romero",  email: "lucia@acme.co",   role: "Member", initials: "LR", you: false },
    { name: "Kenji Sato",    email: "kenji@acme.co",   role: "Admin",  initials: "KS", you: false }
  ];

  var CONNECTIONS = [
    { channel: 0, connected: true,  account: "@acmecorp",        lastSync: "2 min ago"  },
    { channel: 1, connected: true,  account: "@acme.official",   lastSync: "4 min ago"  },
    { channel: 2, connected: true,  account: "Acme Corp",        lastSync: "11 min ago" },
    { channel: 3, connected: true,  account: "@acmedrops",       lastSync: "1 hr ago"   },
    { channel: 4, connected: false, account: "",                 lastSync: ""           }
  ];

  var BILLING = {
    plan: "Pro", price: "$149/mo", renews: "Jul 4, 2026",
    usage: [
      { label: "Leads this cycle",   used: 12847, cap: 20000 },
      { label: "Active campaigns",   used: 23,    cap: 50    },
      { label: "Team seats",         used: 5,     cap: 10    },
      { label: "Connected channels", used: 4,     cap: 5     }
    ],
    plans: [
      { name: "Starter", price: "$29/mo",  leads: "2,500 leads",  campaigns: "10 campaigns", seats: "2 seats",  current: false },
      { name: "Growth",  price: "$79/mo",  leads: "8,000 leads",  campaigns: "25 campaigns", seats: "5 seats",  current: false },
      { name: "Pro",     price: "$149/mo", leads: "20,000 leads", campaigns: "50 campaigns", seats: "10 seats", current: true  }
    ]
  };

  window.PulseData = {
    CHANNELS: CHANNELS,
    WORKSPACES: WORKSPACES,
    DASHBOARD: DASHBOARD,
    DASH_CAMPAIGNS: DASH_CAMPAIGNS,
    CAMPAIGNS: CAMPAIGNS,
    LEADS: LEADS,
    REPORTS: REPORTS,
    TEAM: TEAM,
    CONNECTIONS: CONNECTIONS,
    BILLING: BILLING
  };
})();
