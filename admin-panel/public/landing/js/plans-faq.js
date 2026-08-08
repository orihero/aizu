/* ============================================================
   AIZU — Plans + FAQ section behaviour
   Classic script. Depends on gsap / ScrollTrigger / CS.blurReveal /
   CS.scrollReveal / CS.fadeUp / CS.prefersReducedMotion.
   Defines window.CS.initPlans and window.CS.initFaq, nothing else global.
   ============================================================ */

window.CS = window.CS || {};

(function () {
  'use strict';

  CS.initPlans = function () {
    var section = document.querySelector('.section--plans');
    if (!section) return;

    var title = section.querySelector('.plans-title');
    var sub = section.querySelector('.plans-sub');
    var cards = Array.prototype.slice.call(section.querySelectorAll('.plan'));
    var foot = section.querySelector('.plans-foot');

    CS.scrollReveal(section, function () {
      var tl = gsap.timeline();
      if (title) tl.add(CS.blurReveal(title, { granularity: 'words', stagger: 0.03, blur: 10 }), 0);
      if (sub) tl.add(CS.fadeUp(sub, { y: 24 }), 0.12);
      if (cards.length) tl.add(CS.fadeUp(cards, { y: 36, stagger: 0.07 }), 0.18);
      if (foot) tl.add(CS.fadeUp(foot, { y: 20 }), 0.42);
      return tl;
    });
  };

  CS.initFaq = function () {
    var section = document.querySelector('.section--faq');
    if (!section) return;

    var title = section.querySelector('.faq-title');
    var note = section.querySelector('.faq-aside-note');
    var items = Array.prototype.slice.call(section.querySelectorAll('.faq-item'));
    var reduced = CS.prefersReducedMotion();

    CS.scrollReveal(section, function () {
      var tl = gsap.timeline();
      if (title) tl.add(CS.blurReveal(title, { granularity: 'words', stagger: 0.03, blur: 10 }), 0);
      if (note) tl.add(CS.fadeUp(note, { y: 24 }), 0.14);
      if (items.length) tl.add(CS.fadeUp(items, { y: 26, stagger: 0.05 }), 0.16);
      return tl;
    });

    // <details> handles open/close and keyboard a11y on its own; this only
    // adds the height tween and the one-open-at-a-time behaviour. Under
    // reduced motion the native instant toggle is left alone entirely.
    items.forEach(function (item) {
      var answer = item.querySelector('.faq-answer');
      var summary = item.querySelector('summary');
      if (!answer || !summary) return;

      summary.addEventListener('click', function (e) {
        if (reduced) return; // let the browser do its thing

        e.preventDefault();
        var isOpen = item.hasAttribute('open');

        if (!isOpen) {
          items.forEach(function (other) {
            if (other === item || !other.hasAttribute('open')) return;
            var oa = other.querySelector('.faq-answer');
            gsap.to(oa, {
              height: 0,
              opacity: 0,
              duration: 0.32,
              ease: 'power2.inOut',
              onComplete: function () {
                other.removeAttribute('open');
                gsap.set(oa, { height: 'auto', opacity: 1 });
              }
            });
          });

          item.setAttribute('open', '');
          gsap.fromTo(
            answer,
            { height: 0, opacity: 0 },
            { height: 'auto', opacity: 1, duration: 0.38, ease: 'power3.out' }
          );
        } else {
          gsap.to(answer, {
            height: 0,
            opacity: 0,
            duration: 0.32,
            ease: 'power2.inOut',
            onComplete: function () {
              item.removeAttribute('open');
              gsap.set(answer, { height: 'auto', opacity: 1 });
            }
          });
        }
      });
    });
  };
})();
