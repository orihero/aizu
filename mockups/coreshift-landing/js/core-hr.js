/* ============================================================
   CoreShift — "Core HR solutions" section behaviour.
   Classic script. Depends on gsap / ScrollTrigger globals plus
   CS.splitChars/splitWords/blurReveal/scrollReveal/fadeUp from
   js/split.js + js/reveal.js (loaded before this file).
   ============================================================ */

window.CS = window.CS || {};

CS.initCoreHR = function () {
  var section = document.querySelector('.section--core-hr');
  if (!section) return;

  var badge = section.querySelector('.core-hr-badge');
  var heading = section.querySelector('.core-hr-heading');
  var sub = section.querySelector('.core-hr-sub');
  var button = section.querySelector('.core-hr-cta');
  var ellipses = Array.prototype.slice.call(section.querySelectorAll('.core-hr-ellipse'));

  var reduced = CS.prefersReducedMotion();

  /* ---------- Entrance: badge pop, ellipse groups fade up, heading/sub
     blur-reveal, button tint-fade. All scroll-triggered once. The fade
     targets the two ellipse wrappers (not the individual cards) so it
     composes cleanly with the per-card orbit transform set below —
     wrapper-level y/opacity/scale and child-level x/y never fight over
     the same transform. ---------- */
  CS.scrollReveal(
    section,
    function () {
      var tl = gsap.timeline();

      if (ellipses.length) {
        tl.add(CS.fadeUp(ellipses, { y: 40, stagger: 0.12, duration: 0.8, ease: 'power3.out' }), 0);
      }

      if (badge) {
        tl.fromTo(
          badge,
          { scale: 0.4, opacity: 0 },
          { scale: 1, opacity: 1, duration: 0.7, ease: 'back.out(1.7)' },
          0.1
        );
      }

      if (heading) {
        tl.add(CS.blurReveal(heading, { granularity: 'chars', blur: 12, stagger: 0.018, duration: 0.55 }), 0.25);
      }

      if (sub) {
        tl.add(CS.blurReveal(sub, { granularity: 'words', blur: 8, stagger: 0.008, duration: 0.5 }), 0.45);
      }

      if (button) {
        tl.fromTo(button, { opacity: 0.35 }, { opacity: 1, duration: 0.5, ease: 'power2.out' }, 0.65);
      }

      return tl;
    },
    { start: 'top 78%' }
  );

  if (!ellipses.length) return;

  /* ---------- Orbit setup ----------
     Each ellipse holds 6 cards spaced 60deg apart. Every frame every
     card's x/y is derived from one shared angle plus its own fixed
     offset — x = cx + rx*cos(a), y = cy + ry*sin(a) — so the whole
     group reads as cards travelling clockwise around a flat ellipse,
     not six independently tweened elements. cx/cy are recomputed from
     the section box (cx from the ellipse's data-cx percentage, cy is
     always the section's vertical centre); rx/ry come back from the
     --rx/--ry custom properties so the responsive breakpoints in
     css/core-hr.css apply without any JS-side duplication. */
  var groups = ellipses.map(function (ellipseEl) {
    var cards = Array.prototype.slice.call(ellipseEl.querySelectorAll('.core-hr-card'));
    /* parseFloat("0") is falsy, so `|| 50` would silently discard an
       edge-anchored centre (data-cx="0") and fall back to 50% — guard
       with isNaN instead so 0 and 100 (viewport edges) survive. */
    var cxAttr = parseFloat(ellipseEl.getAttribute('data-cx'));
    return {
      el: ellipseEl,
      cxPct: (isNaN(cxAttr) ? 50 : cxAttr) / 100,
      orbit: { cx: 0, cy: 0, rx: 130, ry: 230 },
      cards: cards.map(function (cardEl, i) {
        return { el: cardEl, offset: i * 60 - 90 };
      })
    };
  });

  function layout() {
    var width = section.offsetWidth;
    var height = section.offsetHeight;

    groups.forEach(function (group) {
      // Read the radii off a zero-visibility probe element sized
      // width:var(--rx); height:var(--ry) rather than off the custom
      // properties directly. getPropertyValue('--rx') hands back the
      // *specified* token -- literally "clamp(240px, 20vw, 340px)" --
      // because unregistered custom properties are never computed, so
      // parseFloat returned NaN and the orbit silently collapsed to the
      // 130px fallback. Measuring the probe gives real resolved pixels and
      // keeps the CSS (including its media queries) the single source of
      // truth for the geometry.
      var probe = group.el.querySelector('.core-hr-metrics');
      var rx = NaN;
      var ry = NaN;
      if (probe) {
        var box = probe.getBoundingClientRect();
        rx = box.width;
        ry = box.height;
      }
      group.orbit.cx = width * group.cxPct;
      group.orbit.cy = height / 2;
      group.orbit.rx = isNaN(rx) || !rx ? group.orbit.rx : rx;
      group.orbit.ry = isNaN(ry) || !ry ? group.orbit.ry : ry;
    });
  }

  function renderFrame(angleDeg) {
    groups.forEach(function (group) {
      var orbit = group.orbit;
      group.cards.forEach(function (card) {
        var t = ((angleDeg + card.offset) * Math.PI) / 180;
        var x = orbit.cx + orbit.rx * Math.cos(t);
        var y = orbit.cy + orbit.ry * Math.sin(t);
        var scale = 1 + 0.035 * Math.sin(t);
        gsap.set(card.el, { x: x, y: y, xPercent: -50, yPercent: -50, scale: scale });
      });
    });
  }

  layout();
  renderFrame(0);

  if (reduced) {
    /* Freeze the orbit at its starting arrangement — no rotation, no
       scroll parallax churn — cards stay put but stay visible with
       their bottom blur intact. */
    return;
  }

  var resizeTimer = null;
  window.addEventListener('resize', function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(layout, 150);
  });

  /* ---------- Continuous clockwise orbit ----------
     One shared angle proxy driven at a constant angular velocity
     (a full revolution roughly every 34s), ease 'none', repeat -1. */
  var angleProxy = { a: 0 };
  gsap.to(angleProxy, {
    a: 360,
    duration: 34,
    ease: 'none',
    repeat: -1,
    onUpdate: function () {
      renderFrame(angleProxy.a);
    }
  });

  /* ---------- Scrubbed scroll parallax on top of the orbit ----------
     Applied to the ellipse wrapper (not the cards), so it composes
     cleanly with each card's own orbit transform. */
  groups.forEach(function (group) {
    var parallax = parseFloat(group.el.getAttribute('data-parallax'));
    if (!parallax) return;

    gsap.to(group.el, {
      yPercent: parallax,
      ease: 'none',
      scrollTrigger: {
        trigger: section,
        start: 'top bottom',
        end: 'bottom top',
        scrub: true
      }
    });
  });
};
