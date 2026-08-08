/* ============================================================
   CoreShift — "Built for everyone" bento section behaviour
   Classic script. Exposes window.CS.initBento only.
   ============================================================ */

window.CS = window.CS || {};

(function () {
  'use strict';

  CS.initBento = function () {
    var section = document.querySelector('.section--bento');
    if (!section) return;

    var reduced = CS.prefersReducedMotion();

    /* ---------------------------------------------------------
       Heading + sub — signature blur reveal, scroll triggered
       --------------------------------------------------------- */
    var title = section.querySelector('.bento-title');
    var sub = section.querySelector('.bento-sub');

    if (title) {
      CS.scrollReveal(
        title,
        function () {
          return CS.blurReveal(title, { blur: 12, stagger: 0.018, duration: 0.55 });
        },
        { start: 'top 85%' }
      );
    }
    if (sub) {
      CS.scrollReveal(
        sub,
        function () {
          return CS.blurReveal(sub, {
            blur: 8,
            stagger: 0.008,
            duration: 0.5,
            delay: 0.12
          });
        },
        { start: 'top 88%' }
      );
    }

    /* ---------------------------------------------------------
       Cards — fade up, staggered, per row
       --------------------------------------------------------- */
    var rows = section.querySelectorAll('.bento-row');
    rows.forEach(function (row) {
      var cards = row.querySelectorAll('.bento-card');
      if (!cards.length) return;
      CS.scrollReveal(
        row,
        function () {
          return CS.fadeUp(cards, { y: 48, scale: 0.97, duration: 0.8, stagger: 0.09 });
        },
        { start: 'top 85%' }
      );
    });

    /* ---------------------------------------------------------
       Card 1 — Attendance Report bars grow from the baseline
       --------------------------------------------------------- */
    var hrCard = section.querySelector('.bento-card--hr');
    if (hrCard) {
      var hrBars = hrCard.querySelectorAll('.bento-bar');
      CS.scrollReveal(
        hrCard,
        function () {
          var tl = gsap.timeline({ delay: 0.32 });
          tl.to(hrBars, {
            scaleY: 1,
            duration: 0.55,
            ease: 'power2.out',
            stagger: 0.06
          });
          return tl;
        },
        { start: 'top 85%' }
      );
    }

    /* ---------------------------------------------------------
       Card 4 — Training Participation bars grow
       --------------------------------------------------------- */
    var dataCard = section.querySelector('.bento-card--data');
    if (dataCard) {
      var trainBars = dataCard.querySelectorAll('.bento-trainbar');
      var trainPill = dataCard.querySelector('.bento-trainbar__pill');
      CS.scrollReveal(
        dataCard,
        function () {
          var tl = gsap.timeline({ delay: 0.4 });
          tl.to(trainBars, {
            scaleY: 1,
            duration: 0.6,
            ease: 'power2.out',
            stagger: 0.07
          });
          if (trainPill) {
            tl.to(trainPill, { opacity: 1, duration: 0.35, ease: 'power2.out' }, '-=0.2');
          }
          return tl;
        },
        { start: 'top 85%' }
      );
    }

    /* ---------------------------------------------------------
       Card 2 — vertical pill roller
       --------------------------------------------------------- */
    initRoller(section, reduced);

    /* ---------------------------------------------------------
       Card 3 — idle floating shield badge
       --------------------------------------------------------- */
    var shield = section.querySelector('.bento-badge--shield');
    if (shield && !reduced) {
      gsap.to(shield, {
        y: -9,
        duration: 2.6,
        ease: 'sine.inOut',
        yoyo: true,
        repeat: -1
      });
    }

    /* ---------------------------------------------------------
       Card 5 — orbiting avatar ring
       --------------------------------------------------------- */
    var ring = section.querySelector('.bento-ring');
    if (ring && !reduced) {
      var inners = ring.querySelectorAll('.bento-ring__avatar-inner');
      gsap.to(ring, { rotation: 360, duration: 40, ease: 'none', repeat: -1 });
      gsap.to(inners, { rotation: -360, duration: 40, ease: 'none', repeat: -1 });
    }
  };

  /* =============================================================
     Pill roller — builds a 7-slot repeating track (3 unique items,
     first + last duplicated as loop padding) and rolls it up one
     slot every 1.6s, resetting seamlessly once a full cycle passes.
     ============================================================= */
  function initRoller(section, reduced) {
    var roller = section.querySelector('.bento-roller');
    var track = section.querySelector('[data-bento-roller-track]');
    if (!roller || !track) return;

    var items = [
      {
        cls: 'cyan',
        text: 'Access Real-Time Insights',
        icon:
          '<circle cx="8" cy="8" r="5.5" stroke="currentColor" stroke-width="1.6"/><path d="M8 5v3l2 2" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>'
      },
      {
        cls: 'coral',
        text: 'Make Data-Driven Decisions',
        icon:
          '<path d="M3 3v10h10" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/><rect x="5" y="8" width="1.8" height="3.5" fill="currentColor"/><rect x="7.6" y="6" width="1.8" height="5.5" fill="currentColor"/><rect x="10.2" y="4" width="1.8" height="7.5" fill="currentColor"/>'
      },
      {
        cls: 'yellow',
        text: 'Track Performance in Real Time',
        icon:
          '<path d="M2 12l3.5-4 3 3L14 4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>'
      }
    ];

    // padded sequence: [yellow, cyan, coral, yellow, cyan, coral, yellow]
    var sequence = [items[2], items[0], items[1], items[2], items[0], items[1], items[2]];

    var slotHeight = 64;
    track.innerHTML = '';
    var pills = sequence.map(function (item) {
      var pill = document.createElement('div');
      pill.className = 'bento-pill';
      pill.innerHTML =
        '<span class="bento-pill__icon bento-pill__icon--' +
        item.cls +
        '"><svg viewBox="0 0 16 16" width="14" height="14" fill="none" aria-hidden="true">' +
        item.icon +
        '</svg></span>' +
        '<span class="bento-pill__text">' +
        item.text +
        '</span>';
      track.appendChild(pill);
      return pill;
    });

    pills.forEach(function (pill, i) {
      gsap.set(pill, { top: i * slotHeight + 'px' });
    });

    var rollerHeight = 176;
    var pillHeight = 56;
    var baseOffset = (rollerHeight - pillHeight) / 2;

    function styleFor(centerIndex, instant) {
      pills.forEach(function (pill, i) {
        var dist = Math.abs(i - centerIndex);
        var opacity = dist === 0 ? 1 : dist === 1 ? 0.35 : 0;
        var scale = dist === 0 ? 1 : dist === 1 ? 0.86 : 0.8;
        var vars = {
          opacity: opacity,
          scale: scale,
          // real black on an ink ground; a near-black shadow would be invisible
          boxShadow: dist === 0 ? '0 16px 34px rgba(0,0,0,.5)' : '0 8px 18px rgba(0,0,0,.32)'
        };
        if (instant || reduced) {
          gsap.set(pill, vars);
        } else {
          gsap.to(pill, Object.assign({ duration: 0.7, ease: 'power3.inOut' }, vars));
        }
      });
    }

    var centerIndex = 1; // start centred on the first "cyan" slot

    function setTrackY(index, animate) {
      var y = baseOffset - index * slotHeight;
      if (animate) {
        gsap.to(track, { y: y, duration: 0.7, ease: 'power3.inOut' });
      } else {
        gsap.set(track, { y: y });
      }
    }

    setTrackY(centerIndex, false);
    styleFor(centerIndex, true);

    if (reduced) return;

    function rollOnce() {
      centerIndex += 1;
      setTrackY(centerIndex, true);
      styleFor(centerIndex, false);

      if (centerIndex >= 4) {
        // index 4 is content-identical to index 1 (seamless loop point) —
        // snap back with NO tween so the reset is imperceptible.
        gsap.delayedCall(0.72, function () {
          centerIndex = 1;
          setTrackY(centerIndex, false);
          styleFor(centerIndex, true);
        });
      }
    }

    gsap.delayedCall(1.6, function tick() {
      rollOnce();
      gsap.delayedCall(1.6, tick);
    });
  }
})();
