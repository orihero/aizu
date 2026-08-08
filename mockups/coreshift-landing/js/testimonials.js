/* ============================================================
   CoreShift — "Words of Appreciation" testimonials section
   Envelope reveal (scroll-triggered, once) + 3D coverflow carousel
   (auto-advance + prev/next). Classic script.
   ============================================================ */

window.CS = window.CS || {};

CS.initTestimonials = function () {
  var section = document.querySelector('.section--testimonials');
  if (!section) return;

  var reduced = CS.prefersReducedMotion();

  /* ---------- heading blur reveal (signature effect) ---------- */
  var title = section.querySelector('.testimonials-title');
  var lead = section.querySelector('.testimonials-lead');

  if (title) {
    CS.scrollReveal(
      title,
      function () {
        return CS.blurReveal(title, { granularity: 'chars', blur: 12, stagger: 0.018 });
      },
      { start: 'top 82%' }
    );
  }

  if (lead) {
    CS.scrollReveal(
      lead,
      function () {
        return CS.blurReveal(lead, { granularity: 'words', blur: 8, stagger: 0.008, delay: 0.12 });
      },
      { start: 'top 82%' }
    );
  }

  /* ---------- carousel setup ---------- */
  var stage = section.querySelector('.testimonials-stage');
  var envelope = section.querySelector('.testimonials-envelope');
  var envelopeBody = section.querySelector('.testimonials-envelope-body');
  var flapLeft = section.querySelector('.testimonials-flap--left');
  var flapRight = section.querySelector('.testimonials-flap--right');
  var pocket = section.querySelector('.testimonials-pocket');
  var cards = Array.prototype.slice.call(section.querySelectorAll('.testimonials-card'));
  var controls = section.querySelector('.testimonials-controls');
  var prevBtn = section.querySelector('.testimonials-btn--prev');
  var nextBtn = section.querySelector('.testimonials-btn--next');

  if (!stage || !cards.length) return;

  var N = cards.length;
  var current = 0;
  var isAnimating = false;
  var autoTimer = null;

  function stageWidth() {
    return stage.getBoundingClientRect().width || 1200;
  }

  /* base + wing card footprints, derived from stage width so the
     carousel scales sensibly at 1280 / 1920 and narrower viewports */
  function sizes() {
    var w = stageWidth();
    var cw = Math.max(300, Math.min(420, w * 0.84));
    var ch = cw * (470 / 420);
    var ww = cw * (470 / 420);
    var wh = ww * (560 / 470);
    return { cw: cw, ch: ch, ww: ww, wh: wh, w: w };
  }

  function relOf(idx) {
    var rel = idx - current;
    rel = ((rel % N) + N) % N;
    if (rel > N / 2) rel -= N;
    return rel;
  }

  function slotFor(rel) {
    var s = sizes();
    if (rel === 0) {
      return {
        x: 0, y: 0, rotateY: 0, rotateZ: 0, z: 0,
        width: s.cw, height: s.ch, opacity: 1, contentOpacity: 1,
        zIndex: 5, center: true
      };
    }
    if (Math.abs(rel) === 1) {
      var sign = rel > 0 ? 1 : -1;
      return {
        x: sign * s.w * 0.32, y: 8, rotateY: -sign * 26, rotateZ: sign * 2, z: -220,
        width: s.ww, height: s.wh, opacity: 1, contentOpacity: 0,
        zIndex: 3, center: false
      };
    }
    var sign2 = rel > 0 ? 1 : -1;
    return {
      x: sign2 * s.w * 0.55, y: 12, rotateY: -sign2 * 32, rotateZ: sign2 * 3, z: -340,
      width: s.ww, height: s.wh, opacity: 0, contentOpacity: 0,
      zIndex: 1, center: false
    };
  }

  function applyState(card, state, animate) {
    gsap.set(card, { zIndex: state.zIndex });
    card.classList.toggle('is-center', !!state.center);
    var content = card.querySelector('.testimonials-card-content');

    if (animate && !reduced) {
      gsap.to(card, {
        x: state.x, y: state.y, rotateY: state.rotateY, rotateZ: state.rotateZ, z: state.z,
        width: state.width, height: state.height, opacity: state.opacity,
        duration: 1, ease: 'power3.inOut'
      });
      if (content) {
        if (state.contentOpacity) {
          gsap.to(content, { opacity: 1, duration: 0.5, delay: 0.5, ease: 'power2.out' });
        } else {
          gsap.to(content, { opacity: 0, duration: 0.32, delay: 0, ease: 'power2.out' });
        }
      }
    } else {
      gsap.set(card, {
        x: state.x, y: state.y, rotateY: state.rotateY, rotateZ: state.rotateZ, z: state.z,
        width: state.width, height: state.height, opacity: state.opacity
      });
      if (content) gsap.set(content, { opacity: state.contentOpacity });
    }
  }

  function render(animate) {
    cards.forEach(function (card, idx) {
      applyState(card, slotFor(relOf(idx)), animate);
    });
  }

  function goTo(newIndex, animate) {
    if (isAnimating && animate) return;
    current = ((newIndex % N) + N) % N;
    if (animate) {
      isAnimating = true;
      render(true);
      gsap.delayedCall(1, function () { isAnimating = false; });
    } else {
      render(false);
    }
  }

  function stopAuto() {
    if (autoTimer) {
      clearInterval(autoTimer);
      autoTimer = null;
    }
  }

  function startAuto() {
    if (reduced) return;
    stopAuto();
    autoTimer = setInterval(function () {
      goTo(current + 1, true);
    }, 3200);
  }

  function restartAuto() {
    if (reduced) return;
    stopAuto();
    startAuto();
  }

  if (prevBtn) {
    prevBtn.addEventListener('click', function () {
      goTo(current - 1, !reduced);
      restartAuto();
    });
  }
  if (nextBtn) {
    nextBtn.addEventListener('click', function () {
      goTo(current + 1, !reduced);
      restartAuto();
    });
  }

  window.addEventListener('resize', function () {
    render(false);
  });

  /* base centring transform — the rest (x/y/rotate/z/scale-via-size) is
     driven entirely by JS/GSAP from here on. Cards are direct children of
     .testimonials-stage, which owns `perspective: 1400px` in CSS, so their
     rotateY/z transforms are correctly foreshortened against one shared
     vanishing point (no per-card transformPerspective needed/wanted). */
  gsap.set(cards, { xPercent: -50, yPercent: -50, transformOrigin: '50% 50%' });

  /* ---------- pre-reveal state ----------
     Card starts low and deep inside the pocket, tilted -5deg (kept for
     the whole slide). Its z-index (2) sits above the back panel (1) but
     below the side flaps (4) and front pocket (6), so the pocket's white
     genuinely occludes whatever of the card falls behind it -- no
     clip-path needed, real stacking does the reveal work as the card
     rises past the pocket's fixed seam. */
  var centerSlot = slotFor(0);
  gsap.set(cards[0], {
    x: 0, y: centerSlot.height * 0.26, rotateY: 0, rotateZ: -5, z: 0,
    width: centerSlot.width, height: centerSlot.height, opacity: 1, zIndex: 2
  });
  cards[0].classList.add('is-center');
  var content0 = cards[0].querySelector('.testimonials-card-content');
  if (content0) gsap.set(content0, { opacity: 1 });

  cards.forEach(function (card, idx) {
    if (idx === 0) return;
    var s = slotFor(relOf(idx));
    gsap.set(card, {
      x: s.x, y: s.y, rotateY: s.rotateY, rotateZ: s.rotateZ, z: s.z,
      width: s.width, height: s.height, opacity: 0, zIndex: s.zIndex
    });
    var c = card.querySelector('.testimonials-card-content');
    if (c) gsap.set(c, { opacity: 0 });
  });

  if (controls) gsap.set(controls, { opacity: 0, y: 16 });

  /* ---------- the envelope-reveal timeline ---------- */
  // triggered off the card itself (not the tall .testimonials-stage / the
  // section) so it starts as the card visibly nears the viewport instead
  // of firing while it is still hundreds of px below the fold
  CS.scrollReveal(
    cards[0],
    function () {
      var tl = gsap.timeline();

      // ~0-1.15s: the card slides up and out of the pocket, tilt held at
      // -5deg the whole way, with a slight overshoot as it settles into
      // place. Nothing else moves yet — the envelope's own pieces are
      // static, so it is purely the card clearing the pocket's fixed
      // white seam that progressively reveals more of the quote as it
      // rises (see the z-index contract in css/testimonials.css).
      tl.to(cards[0], { y: 0, duration: 1.15, ease: 'back.out(1.3)' }, 0);

      // ~1.15-1.65s: only once the card is fully out do the envelope
      // pieces retire — side flaps and front pocket fade and drift
      // down/apart — while the card straightens from -5deg to 0deg and
      // is promoted in front of everything (its carousel x/y/z position
      // was already its final slot the whole time, so "settling into
      // its carousel position" here is just the tilt easing out).
      tl.set(cards[0], { zIndex: 5 }, 1.15);
      tl.to(cards[0], { rotateZ: 0, duration: 0.5, ease: 'power2.out' }, 1.15);

      if (flapLeft) {
        tl.to(flapLeft, { x: -22, y: 14, opacity: 0, duration: 0.55, ease: 'power2.out' }, 1.15);
      }
      if (flapRight) {
        tl.to(flapRight, { x: 22, y: 14, opacity: 0, duration: 0.55, ease: 'power2.out' }, 1.18);
      }
      if (pocket) {
        tl.to(pocket, { y: 26, opacity: 0, duration: 0.55, ease: 'power2.out' }, 1.22);
      }
      if (envelopeBody) {
        tl.to(envelopeBody, { opacity: 0, duration: 0.5, ease: 'power2.out' }, 1.35);
      }

      // neighbouring cards fade in at their 3D positions
      cards.forEach(function (card, idx) {
        if (idx === 0) return;
        tl.to(card, { opacity: 1, duration: 0.4, ease: 'power2.out' }, 1.5);
      });

      // prev/next controls fade up
      if (controls) {
        tl.to(controls, { opacity: 1, y: 0, duration: 0.3, ease: 'power2.out' }, 1.7);
      }

      tl.call(function () {
        if (envelope) gsap.set(envelope, { opacity: 0 });
        startAuto();
      });

      return tl;
    },
    { start: 'top 72%', once: true }
  );
};
