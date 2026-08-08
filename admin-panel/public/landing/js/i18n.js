/* ============================================================
   AIZU landing — translation engine.
   Classic script. Loaded in <head>, AFTER landing/i18n/*.js and
   BEFORE every other landing script, because the section scripts
   (split.js in particular) rewrite the very text nodes this file
   is responsible for filling in. Translate first, animate second.

   Defines window.CS.t / CS.locale / CS.setLocale and nothing else
   global. Depends on nothing — no gsap, no Lenis.

   Markup contract (see index.html):
     data-i18n="key"            -> element.textContent
     data-i18n-html="key.html"  -> element.innerHTML (only <br>/<a>
                                   ever appear in those values)
     data-i18n-attr="alt:key|aria-label:key2"
                                -> setAttribute per pair

   The HTML ships with English already inlined, so a visitor with
   JS disabled still gets a complete, readable page — the English
   dictionary just re-writes what is already there.
   ============================================================ */

window.CS = window.CS || {};

(function () {
  'use strict';

  var DICTS = window.CS_I18N || {};
  var SUPPORTED = ['en', 'ru', 'uz'];
  var FALLBACK = 'en';
  var STORAGE_KEY = 'aizu.lang';

  // <html lang> values. Uzbek is written in Latin script here, hence the
  // explicit subtag: uz-Latn, not a bare uz (which is ambiguous).
  var HTML_LANG = { en: 'en', ru: 'ru', uz: 'uz-Latn' };

  function isSupported(code) {
    return SUPPORTED.indexOf(code) !== -1;
  }

  function readStored() {
    try {
      return window.localStorage.getItem(STORAGE_KEY);
    } catch (err) {
      return null; // private mode / storage disabled — not worth failing over
    }
  }

  function writeStored(code) {
    try {
      window.localStorage.setItem(STORAGE_KEY, code);
    } catch (err) {
      /* same as above: the choice just won't survive the reload */
    }
  }

  // ?lang= wins (so campaign links can force a locale), then the visitor's
  // own stored choice, then what the browser asks for, then English.
  function resolveLocale() {
    var match = /[?&]lang=([a-zA-Z-]+)/.exec(window.location.search);
    if (match) {
      var forced = match[1].toLowerCase().split('-')[0];
      if (isSupported(forced)) return forced;
    }

    var stored = readStored();
    if (stored && isSupported(stored)) return stored;

    var prefs = navigator.languages || [navigator.language || ''];
    for (var i = 0; i < prefs.length; i++) {
      var base = String(prefs[i]).toLowerCase().split('-')[0];
      if (isSupported(base)) return base;
    }

    return FALLBACK;
  }

  var locale = resolveLocale();

  /**
   * CS.t(key) -> string
   * Active locale first, English second, the key itself last. Returning the
   * key (rather than '') makes a missing translation loud in the UI instead
   * of silently blanking an element.
   */
  CS.t = function (key) {
    var active = DICTS[locale];
    if (active && typeof active[key] === 'string') return active[key];

    var base = DICTS[FALLBACK];
    if (base && typeof base[key] === 'string') return base[key];

    return key;
  };

  CS.locale = function () {
    return locale;
  };

  CS.supportedLocales = function () {
    return SUPPORTED.slice();
  };

  function applyAttrSpec(el, spec) {
    // "alt:card.ig1.alt|aria-label:nav.home"
    spec.split('|').forEach(function (pair) {
      var sep = pair.indexOf(':');
      if (sep < 1) return;
      var attr = pair.slice(0, sep).trim();
      var key = pair.slice(sep + 1).trim();
      if (attr && key) el.setAttribute(attr, CS.t(key));
    });
  }

  /**
   * CS.applyTranslations(root) — fill every annotated node under `root`
   * (default: the whole document).
   *
   * Clearing data-cs-split matters: split.js caches its work behind that
   * attribute, so an element re-translated after a split would otherwise
   * keep serving stale per-character spans to CS.splitWords/splitChars.
   */
  CS.applyTranslations = function (root) {
    var scope = root || document;

    Array.prototype.forEach.call(
      scope.querySelectorAll('[data-i18n]'),
      function (el) {
        el.removeAttribute('data-cs-split');
        el.textContent = CS.t(el.getAttribute('data-i18n'));
      }
    );

    Array.prototype.forEach.call(
      scope.querySelectorAll('[data-i18n-html]'),
      function (el) {
        el.removeAttribute('data-cs-split');
        el.innerHTML = CS.t(el.getAttribute('data-i18n-html'));
      }
    );

    Array.prototype.forEach.call(
      scope.querySelectorAll('[data-i18n-attr]'),
      function (el) {
        applyAttrSpec(el, el.getAttribute('data-i18n-attr'));
      }
    );
  };

  function applyDocumentMeta() {
    document.documentElement.setAttribute('lang', HTML_LANG[locale] || locale);
    document.title = CS.t('meta.title');

    var desc = document.querySelector('meta[name="description"]');
    if (desc) desc.setAttribute('content', CS.t('meta.description'));
  }

  function markSwitcher() {
    Array.prototype.forEach.call(
      document.querySelectorAll('[data-lang-option]'),
      function (btn) {
        var code = btn.getAttribute('data-lang-option');
        var active = code === locale;
        btn.classList.toggle('lang-option--active', active);
        btn.setAttribute('aria-pressed', active ? 'true' : 'false');
      }
    );
  }

  /**
   * CS.setLocale(code) — persist the choice and reload.
   *
   * A reload rather than an in-place swap is deliberate. Nine section scripts
   * build paused GSAP timelines over per-character spans produced by split.js;
   * replacing the text under a live timeline strands it on detached nodes and
   * leaves half the page stuck in its pre-reveal state. Re-running the whole
   * boot is the only way to get a correctly choreographed page in the new
   * language, and on a static file that costs nothing worth optimising.
   */
  CS.setLocale = function (code) {
    if (!isSupported(code) || code === locale) return;
    writeStored(code);

    // Drop ?lang= if it is present: it outranks the stored choice on the next
    // resolveLocale(), so leaving it would undo the click that just happened.
    var url = window.location.href;
    if (/[?&]lang=/.test(url)) {
      var cleaned = url
        .replace(/([?&])lang=[^&#]*&/, '$1')
        .replace(/[?&]lang=[^&#]*/, '')
        .replace(/\?(#|$)/, '$1');
      window.location.replace(cleaned);
      return;
    }

    window.location.reload();
  };

  function wireSwitcher() {
    Array.prototype.forEach.call(
      document.querySelectorAll('[data-lang-option]'),
      function (btn) {
        btn.addEventListener('click', function () {
          CS.setLocale(btn.getAttribute('data-lang-option'));
        });
      }
    );
  }

  function boot() {
    applyDocumentMeta();
    CS.applyTranslations();
    markSwitcher();
    wireSwitcher();

    // Release the paint guard set by the inline snippet in <head>. Until this
    // runs the body is visibility:hidden, so no visitor ever sees the English
    // source copy flash before their own language lands on top of it.
    document.documentElement.removeAttribute('data-i18n-pending');
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
