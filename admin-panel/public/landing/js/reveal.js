/* ============================================================
   CoreShift — shared reveal helpers (blur reveal, scroll trigger,
   fade-up). Depends on split.js and the gsap / ScrollTrigger globals.
   Classic script.
   ============================================================ */

window.CS = window.CS || {};

(function () {
  'use strict';

  CS.prefersReducedMotion = function () {
    return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  };

  function resolveTargets(target) {
    if (!target) return [];
    if (typeof target === 'string') {
      return Array.prototype.slice.call(document.querySelectorAll(target));
    }
    if (target instanceof Element) return [target];
    if (target.length !== undefined) return Array.prototype.slice.call(target);
    return [target];
  }

  /**
   * CS.blurReveal(target, opts) -> gsap.timeline
   * The signature progressive-blur text reveal. Splits `target` itself
   * (or every element in an array/NodeList/selector) into chars or words
   * and animates them from a blurred, dim, dropped state into place.
   *
   * opts: { blur=12, stagger=0.018, duration=0.55, delay=0,
   *         from='start' ('start'|'center'|'end'|index),
   *         granularity='chars' ('chars'|'words'), color (override) }
   */
  CS.blurReveal = function (target, opts) {
    opts = opts || {};
    var granularity = opts.granularity || 'chars';
    var blur = opts.blur != null ? opts.blur : 12;
    var stagger = opts.stagger != null ? opts.stagger : 0.018;
    var duration = opts.duration != null ? opts.duration : 0.55;
    var delay = opts.delay || 0;
    var from = opts.from || 'start';
    var forcedColor = opts.color;

    var els = resolveTargets(target);
    var tl = gsap.timeline({ delay: delay });

    if (CS.prefersReducedMotion()) {
      els.forEach(function (el) {
        var spans = granularity === 'words' ? CS.splitWords(el) : CS.splitChars(el);
        gsap.set(el, { opacity: 1 });
        gsap.set(spans, { opacity: 0, filter: 'none', color: '', yPercent: 0 });
        tl.to(
          spans,
          { opacity: 1, duration: 0.45, ease: 'power2.out', stagger: 0.008 },
          0
        );
      });
      return tl;
    }

    els.forEach(function (el) {
      // Capture the resolved colour BEFORE splitting replaces the text
      // nodes — gsap cannot tween to an empty/inherited colour string.
      var finalColor = forcedColor || getComputedStyle(el).color;
      var spans = granularity === 'words' ? CS.splitWords(el) : CS.splitChars(el);

      gsap.set(spans, {
        opacity: 0,
        filter: 'blur(' + blur + 'px)',
        color: '#3d3d46',
        yPercent: 12
      });

      tl.to(
        spans,
        {
          opacity: 1,
          filter: 'blur(0px)',
          color: finalColor,
          yPercent: 0,
          duration: duration,
          ease: 'power2.out',
          stagger: { each: stagger, from: from }
        },
        0
      );
    });

    return tl;
  };

  /**
   * CS.scrollReveal(el, buildTimeline, opts) -> the timeline buildTimeline() returned (or null)
   * Builds the timeline eagerly (so any gsap.set()/from() calls inside it
   * render the pre-animation state immediately, before anything can flash
   * unstyled), pauses it, then plays it once a ScrollTrigger fires.
   *
   * opts: { start='top 82%', once=true }
   */
  CS.scrollReveal = function (el, buildTimeline, opts) {
    opts = opts || {};
    var start = opts.start || 'top 82%';
    var once = opts.once !== undefined ? opts.once : true;

    if (CS.prefersReducedMotion()) {
      // Play immediately (no scroll-gating, so reduced-motion users never
      // have to scroll to reveal content) but let the timeline actually
      // run so its opacity-fade tweens (see CS.blurReveal / CS.fadeUp)
      // play instead of snapping instantly to the end state.
      return buildTimeline();
    }

    var timeline = buildTimeline();
    if (timeline && timeline.pause) timeline.pause(0);

    ScrollTrigger.create({
      trigger: el,
      start: start,
      once: once,
      onEnter: function () {
        if (timeline && timeline.play) timeline.play();
      }
    });

    return timeline;
  };

  /**
   * CS.fadeUp(targets, opts) -> gsap.timeline
   * y 48 -> 0, opacity 0 -> 1, scale .97 -> 1, .8s power3.out, stagger .09.
   */
  CS.fadeUp = function (targets, opts) {
    opts = opts || {};
    var y = opts.y != null ? opts.y : 48;
    var scaleFrom = opts.scale != null ? opts.scale : 0.97;
    var duration = opts.duration != null ? opts.duration : 0.8;
    var stagger = opts.stagger != null ? opts.stagger : 0.09;
    var ease = opts.ease || 'power3.out';
    var delay = opts.delay || 0;

    var tl = gsap.timeline({ delay: delay });

    if (CS.prefersReducedMotion()) {
      tl.fromTo(
        targets,
        { opacity: 0, y: 0, scale: 1 },
        { opacity: 1, y: 0, scale: 1, duration: 0.45, ease: 'power2.out', stagger: 0.04 }
      );
      return tl;
    }

    tl.fromTo(
      targets,
      { y: y, opacity: 0, scale: scaleFrom },
      { y: 0, opacity: 1, scale: 1, duration: duration, ease: ease, stagger: stagger }
    );

    return tl;
  };
})();
