/* ============================================================
   CoreShift — hero section
   Entrance choreography (plays on load, not on scroll), icon
   roulette, connector line draw-in, idle float loops and mouse
   parallax. Classic script — depends on gsap / CS.blurReveal.
   ============================================================ */

window.CS = window.CS || {};

CS.initHero = function () {
  var section = document.querySelector('.section--hero');
  if (!section) return;

  var reduced = typeof CS.prefersReducedMotion === 'function' && CS.prefersReducedMotion();

  var graph = section.querySelector('.hero-graph');
  var centerPos = section.querySelector('.hero-tile-pos[data-tile="center"]');
  var centerVisual = centerPos.querySelector('.hero-tile');
  var icons = Array.prototype.slice.call(centerVisual.querySelectorAll('.hero-roulette-icon'));

  var satellitePositions = Array.prototype.slice.call(
    section.querySelectorAll('.hero-tile-pos:not([data-tile="center"])')
  );
  var satelliteVisuals = satellitePositions.map(function (pos) {
    return pos.querySelector('.hero-tile');
  });

  var dots = Array.prototype.slice.call(section.querySelectorAll('.hero-dot'));
  var trunkPaths = Array.prototype.slice.call(section.querySelectorAll('[data-line^="trunk-"]'));
  var diagPaths = Array.prototype.slice.call(section.querySelectorAll('[data-line^="diag-"]'));
  var allPaths = trunkPaths.concat(diagPaths);

  var heading = section.querySelector('.hero-heading');
  var lead = section.querySelector('.hero-lead');
  var cta = section.querySelector('.hero-cta');

  var parallaxEls = Array.prototype.slice.call(section.querySelectorAll('.hero-tile-parallax'));

  // Prime the connector paths for a stroke-draw animation.
  allPaths.forEach(function (path) {
    var len = path.getTotalLength();
    path.style.strokeDasharray = len + ' ' + len;
    path.dataset.len = len;
    gsap.set(path, { strokeDashoffset: reduced ? 0 : len });
  });

  // ---------------------------------------------------------
  // Reduced motion: land everything in its final state, no timers.
  // ---------------------------------------------------------
  if (reduced) {
    gsap.set(centerVisual, { scale: 1, opacity: 1 });
    gsap.set(icons, { opacity: 0, scale: 1 });
    gsap.set(icons[icons.length - 1], { opacity: 1 });
    gsap.set(satelliteVisuals, { scale: 1, opacity: 1, y: 0 });
    gsap.set(dots, { scale: 1, opacity: 1 });
    gsap.set(cta, { opacity: 1 });
    if (heading) CS.blurReveal(heading, { stagger: 0.02 });
    if (lead) CS.blurReveal(lead, { stagger: 0.008, blur: 8 });
    return;
  }

  // ---------------------------------------------------------
  // Entrance choreography (~1.5s), see SPEC.md section 2.
  // ---------------------------------------------------------
  gsap.set(centerVisual, { scale: 0.4, opacity: 0 });
  gsap.set(icons, { opacity: 0, scale: 1 });
  gsap.set(icons[0], { opacity: 1 });
  gsap.set(satelliteVisuals, { scale: 0.6, opacity: 0 });
  gsap.set(dots, { scale: 0, opacity: 0, transformOrigin: '50% 50%' });
  gsap.set(cta, { opacity: 0.35 });

  var tl = gsap.timeline({ defaults: { ease: 'power2.out' } });

  // 0.00 — centre tile pops in.
  tl.to(centerVisual, { scale: 1, opacity: 1, duration: 0.55, ease: 'back.out(1.7)' }, 0);

  // 0.00 - 0.75 — icon roulette: instant-cut swaps with a scale pop, ~90ms apart.
  // The pop tween's duration must not exceed the swap interval: if it did,
  // the outgoing icon's tween would still be running (writing opacity:1 on
  // every frame) when the next swap's gsap.set(icons,{opacity:0}) fires, and
  // that stale tween's own completion would re-assert opacity:1 afterwards —
  // leaving it stuck visible forever, alongside whatever swapped in next.
  var SWAP_STEP = 0.09;
  var SWAP_POP_DURATION = 0.075;
  icons.forEach(function (icon, i) {
    if (i === 0) return;
    tl.call(
      function () {
        gsap.killTweensOf(icons);
        gsap.set(icons, { opacity: 0 });
        gsap.fromTo(
          icon,
          { opacity: 1, scale: 1.15 },
          { opacity: 1, scale: 1, duration: SWAP_POP_DURATION, ease: 'power2.out' }
        );
      },
      null,
      i * SWAP_STEP
    );
  });

  // Hard-lock the roulette on the circled check just after the last swap's
  // pop finishes (~0.72s) — belt-and-braces so it can never end up stuck on
  // an intermediate glyph and always lands, permanently, on the check.
  var checkIcon = icons[icons.length - 1];
  tl.call(
    function () {
      gsap.killTweensOf(icons);
      gsap.set(icons, { opacity: 0, scale: 1 });
      gsap.set(checkIcon, { opacity: 1 });
    },
    null,
    (icons.length - 1) * SWAP_STEP + SWAP_POP_DURATION + 0.02
  );

  // 0.15 — trunk lines draw outward from the centre tile.
  tl.to(
    trunkPaths,
    { strokeDashoffset: 0, duration: 0.5, ease: 'power2.inOut' },
    0.15
  );

  // 0.30 — diagonal branch lines draw from the fork points.
  tl.to(
    diagPaths,
    { strokeDashoffset: 0, duration: 0.4, ease: 'power2.inOut' },
    0.3
  );

  // 0.35 - 0.70 — satellites fade + scale in, inner tiles first.
  var innerToOuter = [];
  ['cyan', 'red', 'yellow', 'portrait-right', 'eyes', 'portrait-left'].forEach(function (key) {
    var idx = satellitePositions.findIndex(function (pos) {
      return pos.getAttribute('data-tile') === key;
    });
    if (idx > -1) innerToOuter.push(satelliteVisuals[idx]);
  });
  tl.to(
    innerToOuter,
    { scale: 1, opacity: 1, duration: 0.5, ease: 'back.out(1.4)', stagger: 0.06 },
    0.35
  );

  // 0.70 — violet fork dots pop in.
  tl.to(dots, { scale: 1, opacity: 1, duration: 0.4, ease: 'back.out(3)', stagger: 0.04 }, 0.7);

  // 0.55 — headline blur reveal.
  if (heading) tl.add(CS.blurReveal(heading, { stagger: 0.02 }), 0.55);

  // 0.95 — sub-copy blur reveal.
  if (lead) tl.add(CS.blurReveal(lead, { stagger: 0.008, blur: 8 }), 0.95);

  // 1.15 — CTA fades from washed tint to full colour.
  tl.to(cta, { opacity: 1, duration: 0.4, ease: 'power2.out' }, 1.15);

  // ---------------------------------------------------------
  // Idle float loops — satellites only, once the entrance settles.
  // ---------------------------------------------------------
  tl.call(
    function () {
      satelliteVisuals.forEach(function (el) {
        gsap.fromTo(
          el,
          { y: -6 },
          {
            y: 6,
            duration: gsap.utils.random(3.6, 5.2),
            ease: 'sine.inOut',
            yoyo: true,
            repeat: -1,
            delay: Math.random() * 2
          }
        );
      });
    },
    null,
    1.55
  );

  // ---------------------------------------------------------
  // Mouse parallax over the whole graph (max 10px, outer tiles move more).
  // ---------------------------------------------------------
  parallaxEls.forEach(function (el) {
    var depthAttr = el.getAttribute('data-depth');
    el._csDepth = depthAttr ? parseFloat(depthAttr) : 0.5;
    el._csXTo = gsap.quickTo(el, 'x', { duration: 0.6, ease: 'power3.out' });
    el._csYTo = gsap.quickTo(el, 'y', { duration: 0.6, ease: 'power3.out' });
  });

  function onMove(e) {
    var rect = graph.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    var relX = (e.clientX - rect.left) / rect.width - 0.5;
    var relY = (e.clientY - rect.top) / rect.height - 0.5;
    parallaxEls.forEach(function (el) {
      el._csXTo(relX * 2 * 10 * el._csDepth);
      el._csYTo(relY * 2 * 10 * el._csDepth);
    });
  }

  function onLeave() {
    parallaxEls.forEach(function (el) {
      el._csXTo(0);
      el._csYTo(0);
    });
  }

  graph.addEventListener('mousemove', onMove);
  graph.addEventListener('mouseleave', onLeave);
};
