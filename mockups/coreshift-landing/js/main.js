/* ============================================================
   CoreShift — entry point. Boots Lenis + ScrollTrigger, then hands
   off to each section's CS.initXxx(). Classic script, loaded last.
   ============================================================ */

window.CS = window.CS || {};

(function () {
  'use strict';

  gsap.registerPlugin(ScrollTrigger);

  var reduced = typeof CS.prefersReducedMotion === 'function' && CS.prefersReducedMotion();

  if (!reduced && typeof Lenis !== 'undefined') {
    var lenis = new Lenis({
      lerp: 0.085,
      wheelMultiplier: 1,
      smoothWheel: true
    });

    lenis.on('scroll', ScrollTrigger.update);

    gsap.ticker.add(function (time) {
      lenis.raf(time * 1000);
    });

    gsap.ticker.lagSmoothing(0);

    CS.lenis = lenis;
  }

  function safeInit(name) {
    if (typeof CS[name] === 'function') {
      try {
        CS[name]();
      } catch (err) {
        console.error('[CS] ' + name + '() failed:', err);
      }
    }
  }

  function initAll() {
    safeInit('initNav');
    safeInit('initHero');
    safeInit('initCoreHR');
    safeInit('initBento');
    safeInit('initIntegrations');
    safeInit('initTestimonials');
    safeInit('initPlans');
    safeInit('initFaq');
    safeInit('initFooter');
    ScrollTrigger.refresh();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initAll);
  } else {
    initAll();
  }

  window.addEventListener('load', function () {
    ScrollTrigger.refresh();
  });

  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(function () {
      ScrollTrigger.refresh();
    });
  }
})();
