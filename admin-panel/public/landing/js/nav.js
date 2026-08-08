/* ============================================================
   CoreShift — fixed nav pill behaviour
   Classic script. Depends on split.js/reveal.js (for
   CS.prefersReducedMotion) + the global gsap/ScrollTrigger.
   ============================================================ */

window.CS = window.CS || {};

CS.initNav = function () {
  var pill = document.querySelector('[data-nav-pill]');
  if (!pill) return;

  var reduced = typeof CS.prefersReducedMotion === 'function' && CS.prefersReducedMotion();

  // ---- page-load entrance: y:-24 -> 0, opacity 0 -> 1, 0.6s power3.out ----
  if (typeof gsap !== 'undefined') {
    if (reduced) {
      gsap.set(pill, { opacity: 1, y: 0 });
    } else {
      gsap.fromTo(
        pill,
        { y: -24, opacity: 0 },
        { y: 0, opacity: 1, duration: 0.6, ease: 'power3.out' }
      );
    }
  }

  // ---- shadow deepens once the page has scrolled past 40px ----
  if (typeof ScrollTrigger !== 'undefined') {
    ScrollTrigger.create({
      trigger: document.body,
      start: 'top -40',
      toggleClass: { targets: pill, className: 'nav-pill--scrolled' }
    });
  }
};
