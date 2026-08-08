window.CS = window.CS || {};

CS.initFooter = function () {
  var section = document.querySelector('.section--footer');
  if (!section) return;

  var panel = section.querySelector('.footer-panel');
  var tagline = section.querySelector('.footer-tagline');
  var cols = section.querySelectorAll('.footer-col');
  var social = section.querySelector('.footer-block--social');
  var wordmark = section.querySelector('.footer-wordmark');

  var reduced = CS.prefersReducedMotion();

  // ---------------------------------------------------------------------
  // Content entrance: tagline blur-reveals, link columns + social fade up.
  // ---------------------------------------------------------------------
  if (panel && tagline) {
    CS.scrollReveal(
      panel,
      function () {
        var tl = gsap.timeline();
        tl.add(CS.blurReveal(tagline, { granularity: 'words', stagger: 0.02, blur: 8 }), 0);
        if (cols.length) {
          tl.add(CS.fadeUp(cols, { y: 32, stagger: 0.08 }), 0.12);
        }
        if (social) {
          tl.add(CS.fadeUp(social, { y: 32 }), 0.24);
        }
        return tl;
      },
      { start: 'top 82%' }
    );
  }

  // ---------------------------------------------------------------------
  // Giant wordmark: a played (not scrubbed) per-character drop-in. Each
  // letter of "AIZU" falls from above its final position -- blurred,
  // transparent -- down into place, staggered left to right, so the
  // leading letters have already landed and are sharp while the trailing
  // ones are still arriving. It plays once, when the panel scrolls into
  // view, and then the word sits fully sharp, at rest, for good -- there
  // is no scroll-scrubbed blur-out and no permanent blur at rest.
  // ---------------------------------------------------------------------
  if (!wordmark) return;

  gsap.set(wordmark, { opacity: 1 });

  var chars = CS.splitChars(wordmark);
  if (!chars.length) return;

  if (reduced) {
    // No drop, no blur -- the wordmark is simply present and sharp.
    gsap.set(chars, { opacity: 1, filter: 'none', yPercent: 0 });
    return;
  }

  gsap.set(chars, { opacity: 0, filter: 'blur(22px)', yPercent: -100 });

  var playDrop = function () {
    gsap.to(chars, {
      opacity: 1,
      filter: 'blur(0px)',
      yPercent: 0,
      duration: 0.7,
      ease: 'power3.out',
      stagger: 0.055
    });
  };

  if (typeof ScrollTrigger === 'undefined' || !panel) {
    playDrop();
    return;
  }

  ScrollTrigger.create({
    trigger: panel,
    start: 'top 80%',
    once: true,
    onEnter: playDrop
  });
};
