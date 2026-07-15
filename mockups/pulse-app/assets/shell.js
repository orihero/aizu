/* ============================================================
   LeadFlow · Pulse — shared shell (window.Shell)
   Injects the navigation (3 layout modes: dock / header / sidebar),
   handles layout switching + persistence, workspace switcher,
   and exposes shared animation/chart helpers.
   Classic script — load AFTER assets/data.js, BEFORE page script.
   ============================================================ */
(function () {
  "use strict";

  var D = window.PulseData;
  var REDUCED = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var LAYOUTS = ["dock", "sidebar"];

  /* ================= STORAGE (file://-safe) ================= */
  var mem = {};
  var store = {
    get: function (k) {
      try {
        var v = localStorage.getItem(k);
        if (v !== null) return v;
      } catch (e) { /* fall through */ }
      return Object.prototype.hasOwnProperty.call(mem, k) ? mem[k] : null;
    },
    set: function (k, v) {
      mem[k] = v;
      try { localStorage.setItem(k, v); } catch (e) { /* in-memory only */ }
    }
  };

  /* ================= GENERIC HELPERS ================= */
  function $(id) { return document.getElementById(id); }
  function fmt(n) { return Math.round(n).toLocaleString("en-US"); }
  function easeOut(t) { return 1 - Math.pow(1 - t, 3); }

  var rafTimers = [];
  function clearTimers() {
    rafTimers.forEach(function (t) { cancelAnimationFrame(t.raf); clearTimeout(t.to); });
    rafTimers = [];
  }

  function countUp(el, target, opts) {
    opts = opts || {};
    var dec = opts.dec || 0;
    var dur = opts.dur || 1300;
    var render = function (v) {
      el.textContent = dec > 0 ? v.toFixed(dec) : Math.round(v).toLocaleString("en-US");
    };
    if (REDUCED) { render(target); return; }
    var start = null;
    var timer = { raf: 0, to: 0 };
    function step(ts) {
      if (start === null) start = ts;
      var t = Math.min((ts - start) / dur, 1);
      render(target * easeOut(t));
      if (t < 1) timer.raf = requestAnimationFrame(step);
    }
    timer.raf = requestAnimationFrame(step);
    rafTimers.push(timer);
  }

  function later(fn, ms) {
    if (REDUCED) { fn(); return; }
    var timer = { raf: 0, to: setTimeout(fn, ms) };
    rafTimers.push(timer);
  }

  /* SVG element builder */
  var SVGNS = "http://www.w3.org/2000/svg";
  function el(tag, attrs) {
    var n = document.createElementNS(SVGNS, tag);
    for (var k in attrs) n.setAttribute(k, attrs[k]);
    return n;
  }

  /* Chart tooltip controller — positions a .chart-tip inside a .chart-wrap
     using SVG viewBox coordinates. Pages wire their own hover events. */
  function tipController(wrap, tip, svg, W, H) {
    return {
      show: function (x, y, html) {
        tip.innerHTML = html;
        var wr = wrap.getBoundingClientRect();
        var sr = svg.getBoundingClientRect();
        tip.style.left = ((sr.left - wr.left) + x * (sr.width / W)) + "px";
        tip.style.top  = ((sr.top  - wr.top)  + y * (sr.height / H)) + "px";
        tip.classList.add("show");
      },
      hide: function () { tip.classList.remove("show"); }
    };
  }

  /* Tile pop-in: animate every .tile in scope (stagger via --i),
     releasing the animation on end so :hover lifts keep working. */
  function popTiles(scope) {
    var tiles = (scope || document).querySelectorAll(".tile");
    if (REDUCED) {
      tiles.forEach(function (t) { t.classList.add("pop-in"); t.style.opacity = "1"; });
      return;
    }
    tiles.forEach(function (t) {
      if (!t.dataset.popWired) {
        t.dataset.popWired = "1";
        t.addEventListener("animationend", function (e) {
          if (e.target === t && e.animationName === "tilePop") {
            t.classList.remove("pop-in");
            t.style.opacity = "1";
          }
        });
      }
      t.classList.remove("pop-in");
    });
    void document.body.offsetWidth;
    tiles.forEach(function (t) { t.classList.add("pop-in"); });
  }

  /* Period segmented control: wires any .seg [data-period] buttons.
     Restores the persisted period when this seg has it. Returns the
     initial period; calls cb(period) on every user switch. */
  function initPeriodSeg(cb) {
    var btns = Array.prototype.slice.call(document.querySelectorAll(".seg [data-period]"));
    if (!btns.length) return null;
    var stored = store.get("pulse.period");
    var initial = null;
    btns.forEach(function (b) { if (b.dataset.period === stored) initial = b; });
    if (!initial) initial = document.querySelector(".seg [data-period].active") || btns[btns.length - 1];
    btns.forEach(function (b) {
      var on = b === initial;
      b.classList.toggle("active", on);
      b.setAttribute("aria-selected", on ? "true" : "false");
    });
    var current = initial.dataset.period;
    btns.forEach(function (btn) {
      btn.addEventListener("click", function () {
        if (btn.dataset.period === current) return;
        current = btn.dataset.period;
        store.set("pulse.period", current);
        btns.forEach(function (b) {
          b.classList.toggle("active", b === btn);
          b.setAttribute("aria-selected", b === btn ? "true" : "false");
        });
        cb(current);
      });
    });
    return current;
  }

  /* ================= LAYOUT ================= */
  function getLayout() {
    var l = document.documentElement.getAttribute("data-layout");
    return LAYOUTS.indexOf(l) >= 0 ? l : "dock";
  }

  function syncLayoutSwitch() {
    var mode = getLayout();
    document.querySelectorAll(".lay-btn").forEach(function (b) {
      b.classList.toggle("active", b.dataset.mode === mode);
      b.setAttribute("aria-pressed", b.dataset.mode === mode ? "true" : "false");
    });
  }

  function setLayout(mode, animate) {
    if (LAYOUTS.indexOf(mode) < 0 || mode === getLayout()) return;
    store.set("pulse.layout", mode);
    var html = document.documentElement;
    var nav = $("shellNav");
    var apply = function () {
      html.setAttribute("data-layout", mode);
      syncLayoutSwitch();
      document.dispatchEvent(new CustomEvent("pulse:layout", { detail: { mode: mode } }));
    };
    if (!animate || REDUCED || !nav) { apply(); return; }
    html.classList.add("layout-anim");
    nav.classList.add("nav-fading");
    setTimeout(function () {
      apply();
      void nav.offsetWidth;
      nav.classList.remove("nav-fading");
      setTimeout(function () { html.classList.remove("layout-anim"); }, 360);
    }, 130);
  }

  /* ================= NAV INJECTION ================= */
  var ICONS = {
    dashboard: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/></svg>',
    campaigns: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 11l16-7v16L3 13v-2z"/><path d="M7.5 13.5V18a1.5 1.5 0 0 0 1.5 1.5h1a1.5 1.5 0 0 0 1.5-1.5v-3"/></svg>',
    leads:     '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c.8-3.4 3.4-5.5 6.5-5.5s5.7 2.1 6.5 5.5"/><circle cx="17.5" cy="9.5" r="2.5"/><path d="M16.5 14.6c2.5.3 4.4 2 5 4.9"/></svg>',
    reports:   '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 20V10"/><path d="M10 20V4"/><path d="M16 20v-8"/><path d="M22 20H2"/></svg>',
    settings:  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .34 1.87l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.7 1.7 0 0 0-1.87-.34 1.7 1.7 0 0 0-1.03 1.56V21a2 2 0 1 1-4 0v-.09c0-.7-.42-1.32-1.06-1.58a1.7 1.7 0 0 0-1.84.36l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06c.49-.49.62-1.22.34-1.86A1.7 1.7 0 0 0 3.1 14H3a2 2 0 1 1 0-4h.09c.7 0 1.32-.42 1.58-1.06a1.7 1.7 0 0 0-.36-1.84l-.06-.06A2 2 0 1 1 7.08 4.2l.06.06c.49.49 1.22.62 1.86.34.62-.26 1.03-.86 1.03-1.55V3a2 2 0 1 1 4 0v.09c0 .69.41 1.3 1.04 1.56.64.27 1.37.14 1.86-.35l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.7 1.7 0 0 0-.34 1.87c.26.62.86 1.03 1.55 1.03H21a2 2 0 1 1 0 4h-.09c-.69 0-1.29.41-1.55 1.03z"/></svg>',
    layDock:    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="3.5" width="14" height="5" rx="2.5"/><rect x="3" y="12" width="18" height="8.5" rx="2" opacity=".45"/></svg>',
    laySidebar: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3.5" width="6" height="17" rx="1.5"/><rect x="12.5" y="3.5" width="8.5" height="17" rx="2" opacity=".45"/></svg>',
    check:      '<svg class="ws-check" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg>'
  };

  var PAGES = [
    { id: "dashboard", label: "Dashboard", href: "index.html",     icon: ICONS.dashboard },
    { id: "campaigns", label: "Campaigns", href: "campaigns.html", icon: ICONS.campaigns },
    { id: "leads",     label: "Leads",     href: "leads.html",     icon: ICONS.leads     },
    { id: "reports",   label: "Reports",   href: "reports.html",   icon: ICONS.reports   },
    { id: "settings",  label: "Settings",  href: "settings.html",  icon: ICONS.settings  }
  ];

  function currentWorkspace() {
    var stored = store.get("pulse.workspace");
    for (var i = 0; i < D.WORKSPACES.length; i++) {
      if (D.WORKSPACES[i].name === stored) return D.WORKSPACES[i];
    }
    return D.WORKSPACES[0];
  }

  function activePageId() {
    var fromBody = document.body.getAttribute("data-page");
    if (fromBody) return fromBody;
    var file = (location.pathname.split("/").pop() || "index.html");
    for (var i = 0; i < PAGES.length; i++) {
      if (PAGES[i].href === file) return PAGES[i].id;
    }
    return "dashboard";
  }

  function injectNav() {
    var active = activePageId();
    var ws = currentWorkspace();

    var items = PAGES.map(function (p) {
      return '<a class="nav-item' + (p.id === active ? " active" : "") + '" href="' + p.href + '" data-tip="' + p.label + '" aria-label="' + p.label + '"' + (p.id === active ? ' aria-current="page"' : "") + '>' +
        p.icon + '<span class="nav-label">' + p.label + "</span></a>";
    }).join("");

    var wsItems = D.WORKSPACES.map(function (w) {
      return '<button class="ws-item' + (w.name === ws.name ? " selected" : "") + '" data-ws="' + w.name + '" role="menuitem">' +
        '<span class="ws-glyph ws-glyph-' + w.accent + '">' + w.glyph + "</span>" +
        '<span><span class="ws-item-name">' + w.name + '</span><br><span class="ws-item-meta">' + w.meta + "</span></span>" +
        ICONS.check + "</button>";
    }).join("");

    var html =
      '<nav class="shell-nav" id="shellNav" aria-label="Primary">' +
        '<a class="nav-logo" href="index.html"><span class="dot"></span><span class="name">LeadFlow</span></a>' +
        '<div class="nav-items">' + items + "</div>" +
        '<div class="nav-spacer"></div>' +
        '<div class="nav-tools">' +
          '<div class="lay-switch" role="group" aria-label="Layout">' +
            '<button class="lay-btn" data-mode="dock" title="Dock layout" aria-pressed="false">' + ICONS.layDock + "</button>" +
            '<button class="lay-btn" data-mode="sidebar" title="Sidebar layout" aria-pressed="false">' + ICONS.laySidebar + "</button>" +
          "</div>" +
          '<div class="nav-divider"></div>' +
          '<div class="ws" id="ws">' +
            '<button class="ws-btn" id="wsBtn" aria-haspopup="true" aria-expanded="false">' +
              '<span class="ws-glyph" id="wsGlyph">' + ws.glyph + "</span>" +
              '<span class="ws-name" id="wsName">' + ws.name + "</span>" +
              '<svg class="ws-caret" width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>' +
            "</button>" +
            '<div class="ws-menu" id="wsMenu" role="menu">' + wsItems + "</div>" +
          "</div>" +
          '<div class="nav-user"><span class="nav-avatar" title="Jordan Mercer">JM</span><span class="nav-username">Jordan Mercer</span></div>' +
        "</div>" +
      "</nav>";

    document.body.insertAdjacentHTML("afterbegin", html);

    /* layout quick-switch */
    document.querySelectorAll(".lay-btn").forEach(function (b) {
      b.addEventListener("click", function () { setLayout(b.dataset.mode, true); });
    });
    syncLayoutSwitch();

    /* workspace dropdown */
    var wsBox = $("ws"), wsBtn = $("wsBtn"), wsMenu = $("wsMenu");
    function closeWs() {
      wsBox.classList.remove("open");
      wsBtn.setAttribute("aria-expanded", "false");
    }
    wsBtn.addEventListener("click", function (e) {
      e.stopPropagation();
      var open = wsBox.classList.toggle("open");
      wsBtn.setAttribute("aria-expanded", open ? "true" : "false");
    });
    wsMenu.querySelectorAll(".ws-item").forEach(function (item) {
      item.addEventListener("click", function (e) {
        e.stopPropagation();
        wsMenu.querySelectorAll(".ws-item").forEach(function (i) { i.classList.remove("selected"); });
        item.classList.add("selected");
        applyWorkspace(item.dataset.ws);
        closeWs();
      });
    });
    document.addEventListener("click", function (e) {
      if (!wsBox.contains(e.target)) closeWs();
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") closeWs();
    });
  }

  function applyWorkspace(name) {
    store.set("pulse.workspace", name);
    var ws = currentWorkspace();
    var glyph = $("wsGlyph"), label = $("wsName");
    if (glyph) glyph.textContent = ws.glyph;
    if (label) label.textContent = ws.name;
    document.querySelectorAll("[data-ws-name]").forEach(function (n) { n.textContent = ws.name; });
    document.dispatchEvent(new CustomEvent("pulse:workspace", { detail: { workspace: ws } }));
  }

  /* ================= BOOT ================= */
  injectNav();
  /* reflect persisted workspace into any page placeholders */
  document.querySelectorAll("[data-ws-name]").forEach(function (n) {
    n.textContent = currentWorkspace().name;
  });

  /* ================= EXPORT ================= */
  window.Shell = {
    REDUCED: REDUCED,
    $: $,
    fmt: fmt,
    easeOut: easeOut,
    countUp: countUp,
    later: later,
    clearTimers: clearTimers,
    el: el,
    SVGNS: SVGNS,
    tipController: tipController,
    popTiles: popTiles,
    initPeriodSeg: initPeriodSeg,
    store: store,
    getLayout: getLayout,
    setLayout: setLayout,
    workspace: currentWorkspace
  };
})();
