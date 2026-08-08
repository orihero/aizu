/* ============================================================
   CoreShift — integrations arc carousel
   Classic script. Depends on gsap / ScrollTrigger / CS.blurReveal /
   CS.scrollReveal / CS.prefersReducedMotion (already loaded).
   Defines window.CS.initIntegrations and nothing else global.
   ============================================================ */

window.CS = window.CS || {};

(function () {
  'use strict';

  // The caption deck is rewritten on every rotation step, so its copy cannot
  // live on a data-i18n hook in the markup — it reads the active dictionary
  // through CS.t instead. Built on first use rather than at parse time so the
  // dependency on i18n.js having run is satisfied by call order, not by where
  // this <script> happens to sit in index.html.
  var DECK = null;

  function deck() {
    if (!DECK) {
      DECK = [1, 2, 3, 4, 5, 6].map(function (n) {
        return {
          name: CS.t('integrations.' + n + '.name'),
          desc: CS.t('integrations.' + n + '.desc')
        };
      });
    }
    return DECK;
  }

  // Tiles sit on slots 16deg apart, centred on 0; the deck is exactly as long
  // as the slot list, so the "wheel" is a closed loop: every tile keeps its own
  // identity forever, only its angle changes each step. This keeps the whole
  // thing a genuine rotating wheel (per-frame angle -> position) rather than
  // hard-coded keyframes.
  //
  // The recycle is deliberately TWO-PHASE. A tile leaving the left end first
  // rolls on to -48deg with everything else, fading to nothing on the way, and
  // is only bumped by (slots * 16deg) onto the mirrored +48deg slot once that
  // roll has finished. Both -48 and +48 render at opacity 0, so the bump itself
  // is unobservable. Doing it in one phase — bumping at the start of the step —
  // is what used to empty the leftmost visible slot for the first ~250ms of
  // every cycle, since the replacement tile is still easing across.
  //
  // AIZU rebrand: the deck grew from 5 to 6 (one per source platform). Slots
  // beyond +-48deg render at opacity 0, so the 6th slot is simply parked
  // off-stage until the wheel brings it round. Everything downstream derives
  // from SLOT_ANGLES.length, so the deck can be resized again without further
  // edits — only the markup's tile count has to match.
  var SLOT_ANGLES = [-32, -16, 0, 16, 32, 48];
  var RADIUS = 874; // circle centre sits far below the panel (widened so the
  // 5-tile arc spans ~79% of the panel width, matching the reference)
  var STEP_DEG = 16;
  var HOLD_MS = 1200; // time between the start of each rotation step
  var STEP_DURATION = 1.0; // seconds, power3.inOut
  var WRAP_THRESHOLD = -40; // past the left slot + half a step -> recycle

  // AIZU rebrand: off-centre tiles sit at --ground-3, the centre tile lifts to
  // --ground-4. (Was --tile grey lifting to white on the light theme.)
  var TILE_GREY = { r: 0x20, g: 0x20, b: 0x26 }; // --ground-3
  var TILE_WHITE = { r: 0x2f, g: 0x2f, b: 0x38 }; // --ground-4, lifted
  // The checkpoint numeral rides the same curve: grey when parked, lime when it
  // reaches dead centre — lime arrives on the one tile that is "the signal".
  var NUM_GREY = { r: 0x9a, g: 0x9a, b: 0xa2 }; // --grey-1
  var NUM_LIME = { r: 0xd9, g: 0xf2, b: 0x4f }; // --lime

  function lerp(a, b, t) {
    return a + (b - a) * t;
  }

  function mix(a, b, t) {
    return (
      'rgb(' +
      Math.round(lerp(a.r, b.r, t)) + ',' +
      Math.round(lerp(a.g, b.g, t)) + ',' +
      Math.round(lerp(a.b, b.b, t)) + ')'
    );
  }

  function lerpColor(t) {
    return mix(TILE_GREY, TILE_WHITE, t);
  }

  function lerpNumColor(t) {
    return mix(NUM_GREY, NUM_LIME, t);
  }

  function polar(angleDeg) {
    var rad = (angleDeg * Math.PI) / 180;
    return { x: RADIUS * Math.sin(rad), y: RADIUS * (1 - Math.cos(rad)) };
  }

  // Derives every visual property purely from the current angle so the
  // centre tile (white / ~170px-equivalent / shadow) and the off-centre
  // tiles (grey / 135px / no shadow, opacity 1 / .92 / .55) fall naturally
  // out of one continuous curve — this is what lets the roll animate
  // smoothly through in-between angles instead of snapping.
  function stateForAngle(angle) {
    var a = Math.min(Math.abs(angle), 48);
    var scale, opacity, colorT;
    if (a <= 16) {
      var t1 = a / 16;
      scale = lerp(170 / 135, 1, t1); // 135px base tile, ~170px centre tile
      opacity = lerp(1, 0.92, t1);
      colorT = lerp(1, 0, t1);
    } else if (a <= 32) {
      var t2 = (a - 16) / 16;
      scale = 1;
      opacity = lerp(0.92, 0.55, t2);
      colorT = 0;
    } else {
      var t3 = (a - 32) / 16;
      scale = 1;
      opacity = lerp(0.55, 0, t3);
      colorT = 0;
    }
    return {
      scale: scale,
      opacity: Math.max(0, Math.min(1, opacity)),
      shadow: Math.max(0, 1 - a / 10), // only reads near dead-centre
      colorT: colorT
    };
  }

  CS.initIntegrations = function () {
    var section = document.querySelector('.section--integrations');
    if (!section) return;

    var panel = section.querySelector('.integrations-panel');
    var badge = section.querySelector('.integrations-badge');
    var title = section.querySelector('.integrations-title');
    var tileEls = Array.prototype.slice.call(section.querySelectorAll('.integrations-tile'));
    var nameEl = section.querySelector('.integrations-caption__name');
    var descEl = section.querySelector('.integrations-caption__desc');

    if (!panel || tileEls.length !== SLOT_ANGLES.length) return;

    var reduced = CS.prefersReducedMotion();

    var tiles = tileEls.map(function (el, i) {
      return {
        el: el,
        angle: SLOT_ANGLES[i], // logical resting/target angle
        proxy: { angle: reduced ? SLOT_ANGLES[i] : 0, scale: 1, opacity: 0, shadow: 0, colorT: 0 }
      };
    });

    function applyTile(tile) {
      var p = polar(tile.proxy.angle);
      var el = tile.el;
      el.style.transform =
        'translate(' +
        p.x.toFixed(2) +
        'px,' +
        p.y.toFixed(2) +
        'px) rotate(' +
        tile.proxy.angle.toFixed(2) +
        'deg) scale(' +
        tile.proxy.scale.toFixed(4) +
        ')';
      el.style.opacity = tile.proxy.opacity;
      el.style.background = lerpColor(tile.proxy.colorT);
      // the tile's glyph inherits this via currentColor
      el.style.color = lerpNumColor(tile.proxy.colorT);
      // On an ink ground a near-black shadow is invisible, so the centre tile
      // is separated by a real black shadow plus a lime rim as it settles.
      var alpha = (0.55 * tile.proxy.shadow).toFixed(3);
      el.style.boxShadow =
        tile.proxy.shadow > 0.02
          ? '0 20px 46px rgba(0,0,0,' + alpha + '), 0 0 0 1px rgba(217,242,79,' +
            (0.22 * tile.proxy.shadow).toFixed(3) + ')'
          : 'none';
    }

    function applyAll() {
      tiles.forEach(applyTile);
    }

    function setProxyToAngle(tile, angle) {
      var st = stateForAngle(angle);
      tile.proxy.angle = angle;
      tile.proxy.scale = st.scale;
      tile.proxy.opacity = st.opacity;
      tile.proxy.shadow = st.shadow;
      tile.proxy.colorT = st.colorT;
    }

    function updateCaption(centerDeckIndex, animate) {
      var item = deck()[centerDeckIndex];
      if (!nameEl || !descEl) return;
      if (!animate) {
        nameEl.textContent = item.name;
        descEl.textContent = item.desc;
        return;
      }
      gsap
        .timeline()
        .to([nameEl, descEl], { opacity: 0, y: -8, duration: 0.25, ease: 'power2.in' })
        .call(function () {
          nameEl.textContent = item.name;
          descEl.textContent = item.desc;
        })
        .fromTo(
          [nameEl, descEl],
          { opacity: 0, y: 8 },
          { opacity: 1, y: 0, duration: 0.3, ease: 'power2.out' }
        );
    }

    var step = 0; // centre deck index = (step + 2) % SLOT_ANGLES.length
    var loopTimer = null;

    function doStep() {
      step += 1;
      updateCaption((step + 2) % SLOT_ANGLES.length, true);

      // Tiles leaving the left edge this step. They are NOT relocated yet —
      // see the onComplete below.
      var leaving = [];

      tiles.forEach(function (tile) {
        var nextAngle = tile.angle - STEP_DEG;
        tile.angle = nextAngle;

        if (nextAngle <= WRAP_THRESHOLD) {
          // This tile is walking off the left end. It used to be teleported
          // straight to the right-hand slot right here, on the first frame of
          // the step — which emptied the leftmost visible slot instantly while
          // its replacement only slid across over the following second. Under
          // power3.inOut that replacement covers under 3% of the distance in
          // the first quarter of the roll, so the far-left tile read as simply
          // vanishing for ~250ms of every cycle.
          //
          // Instead it now rolls on to -48deg like any other tile, which is
          // exactly where stateForAngle() reports opacity 0, so it dissolves in
          // place. Only once it is invisible (onComplete) does it jump to the
          // mirrored +48deg parking slot, ready to fade back in from the right
          // on the next step. The jump is unobservable because both ends of it
          // are fully transparent.
          leaving.push(tile);
        }
      });

      var targets = tiles.map(function (t) {
        return stateForAngle(t.angle);
      });

      gsap.to(
        tiles.map(function (t) {
          return t.proxy;
        }),
        {
          angle: function (i) {
            return tiles[i].angle;
          },
          scale: function (i) {
            return targets[i].scale;
          },
          opacity: function (i) {
            return targets[i].opacity;
          },
          shadow: function (i) {
            return targets[i].shadow;
          },
          colorT: function (i) {
            return targets[i].colorT;
          },
          duration: STEP_DURATION,
          ease: 'power3.inOut',
          onUpdate: applyAll,
          onComplete: function () {
            // Park each departed tile on the mirrored right-hand slot. Both
            // -48deg (where it just faded out) and +48deg (where it lands)
            // render at opacity 0, so this is invisible.
            leaving.forEach(function (tile) {
              tile.angle += SLOT_ANGLES.length * STEP_DEG; // -48 -> +48
              setProxyToAngle(tile, tile.angle);
              applyTile(tile);
            });

            // Snap every tile onto its exact resting state, so repeated steps
            // can't accumulate easing float drift.
            tiles.forEach(function (tile) {
              setProxyToAngle(tile, tile.angle);
            });
            applyAll();
          }
        }
      );
    }

    function startLoop() {
      if (reduced || loopTimer) return;
      loopTimer = setInterval(doStep, HOLD_MS);
    }

    if (reduced) {
      // Static, fully-arranged, no auto-advance — all content still present.
      tiles.forEach(function (tile) {
        setProxyToAngle(tile, tile.angle);
        applyTile(tile);
      });
      if (badge) gsap.set(badge, { opacity: 1, scale: 1 });
      if (panel) gsap.set(panel, { opacity: 1, y: 0 });
      if (title) CS.blurReveal(title, { blur: 12, stagger: 0.018, duration: 0.55 });
      if (nameEl && descEl) gsap.set([nameEl, descEl], { opacity: 1, y: 0 });
      updateCaption((step + 2) % SLOT_ANGLES.length, false);
      return;
    }

    // Pre-render the "collapsed at centre" starting state immediately so
    // there's no flash before the scroll trigger fires.
    tiles.forEach(applyTile);
    gsap.set(panel, { opacity: 0, y: 56 });
    gsap.set(badge, { opacity: 0, scale: 0.4 });
    if (nameEl && descEl) gsap.set([nameEl, descEl], { opacity: 0, y: 8 });
    updateCaption((step + 2) % SLOT_ANGLES.length, false);

    CS.scrollReveal(
      section,
      function () {
        var tl = gsap.timeline();

        tl.to(panel, { opacity: 1, y: 0, duration: 0.9, ease: 'power3.out' }, 0);
        tl.to(badge, { opacity: 1, scale: 1, duration: 0.6, ease: 'back.out(1.7)' }, 0.1);
        tl.add(CS.blurReveal(title, { blur: 12, stagger: 0.018, duration: 0.55 }), 0.25);

        var fanTargets = tiles.map(function (t) {
          return stateForAngle(t.angle);
        });
        tl.to(
          tiles.map(function (t) {
            return t.proxy;
          }),
          {
            angle: function (i) {
              return tiles[i].angle;
            },
            scale: function (i) {
              return fanTargets[i].scale;
            },
            opacity: function (i) {
              return fanTargets[i].opacity;
            },
            shadow: function (i) {
              return fanTargets[i].shadow;
            },
            colorT: function (i) {
              return fanTargets[i].colorT;
            },
            duration: 0.9,
            ease: 'power3.out',
            stagger: { each: 0.05, from: 'center' },
            onUpdate: applyAll
          },
          0.55
        );

        if (nameEl && descEl) {
          tl.to([nameEl, descEl], { opacity: 1, y: 0, duration: 0.5, ease: 'power2.out' }, 1.15);
        }

        tl.call(startLoop);

        return tl;
      },
      { start: 'top 78%' }
    );
  };
})();
