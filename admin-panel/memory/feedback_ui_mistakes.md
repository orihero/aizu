# UI Mistakes Log

## 1. Ambiguous accessible name: "Live" run pill collided with the "Live" status-filter chip

**Why:** The Campaigns page already renders a status-filter chip labeled "Live"
(button role). The new run control reused the bare visible word "Live" for its
live-run affordance, giving two `button`s with the identical accessible name
"Live" on the same page. RTL `getByRole('button', { name: 'Live' })` then matched
both, and a screen-reader user would hear two indistinguishable "Live" buttons.

**How to apply:** When adding a control whose visible text duplicates an existing
control's label on the same view, give the new control a distinct `aria-label`
(here `aria-label="Run live"`) while keeping the short visible text. Before
choosing a label, scan the page for existing buttons/chips/links with the same
word (filter chips, tabs, status pills are common offenders) and disambiguate.

## 2. `step` on `<input type="number">` silently blocked form submission for off-grid values

**Why:** The campaign form (and Settings) set `step={100}` (budget), `step={10}`
(goal) and `step={0.05}` (threshold) intending them only as spinner increments.
But HTML5 treats `step` as a *constraint*: any value not on the grid (e.g. a
budget of 5250, a goal of 175, a threshold of 0.72) sets `validity.stepMismatch`,
so the browser refuses to submit and shows its native "Please enter a valid
value" bubble. The form's `onSubmit` (and thus the mutation) never fires, so the
write never reaches the server — it reads to the user as a "validation error" on
create. The default values (7500 / 200 / 0.7) happen to sit on the grid, which
is why it worked sometimes and the jsdom tests (which don't enforce constraint
validation on submit) stayed green.

**How to apply:** For free-numeric fields use `step="any"` and keep `min`/`max`
for the real range guard (negatives, >1). Reserve a fixed `step` only when the
value is genuinely quantized (and accept that off-grid input is then rejected).
When testing number inputs, assert `(input as HTMLInputElement).validity.valid`
for a realistic off-grid value — jsdom models `stepMismatch` even though it
won't block the submit the way a real browser does, so a click-through test
alone will not catch this.

## 3. Flex child with `truncate` but no `min-w-0` overflowed the fixed-width sidebar

**Why:** The sidebar campaign `<select>` used `grow truncate` to fit a fixed
`w-[244px]` rail. But a flex item defaults to `min-width: auto`, so it refuses to
shrink below its content's intrinsic width — and a `<select>`'s intrinsic width is
the widest `<option>`. A long campaign name therefore pushed the control past the
sidebar edge, and `truncate` (which only ellipsizes once the box is actually
narrower than its text) never engaged. Short default names hid the bug.

**How to apply:** Any element with `truncate`/`overflow-hidden` that lives inside a
flex row AND must shrink also needs `min-w-0` (the canonical Tailwind fix:
`min-w-0 grow truncate`). This applies doubly to `<select>`/`<input>`, whose
intrinsic min-width comes from their content (longest option / placeholder). When a
truncating element sits in a fixed-width container, verify with a deliberately long
value, not the default.

## 4. Drove a persistent "Starting…" spinner off a sticky `mutation.isSuccess` → stuck "loading" forever

**Why:** In the RunDrawer I made Start keep the drawer open and showed a "Starting
run…" spinner gated on `runCampaign.isSuccess`. A React Query mutation's `isSuccess`
is **sticky** — it stays true until `reset()`/unmount, not a transient pulse. So once
a run was accepted, the spinner never cleared: if the run halted instantly (or any
time after it ended) and the drawer fell back to the non-running branch, it showed a
forever "loading" state. Compounded by `/api/state` polling only every 30s, so
`run.active` (the "Running…" label + Stop view) also lagged ~30s behind a real halt —
the card kept spinning "Running…" long after the backend halted.

**How to apply:** Never bind a *persistent* UI state to a mutation's sticky
`isSuccess`/`isError` — use `isPending` for the in-flight pulse, and derive lasting
state from the real domain data (here `run.active` from `/api/state` + the activity
feed's `finished`). If you keep a mutation result across an open surface, `reset()` it
on close. For "is X still happening?" UIs, (a) poll the authoritative source faster
while the thing is active (dynamic `refetchInterval` — fast when `active`, idle
otherwise) and (b) invalidate that source the moment a cheaper live signal says it's
done (here: feed `finished` → invalidate panel state) instead of waiting for the slow
poll. Always test the *short/instant* path (a run that ends in <1 poll), not just the
happy long-running one — the bug only appears when the activity ends fast.

## 5. Passing an AI draft into an already-mounted form did nothing — `useState(initializer)` reads only once

**Why:** The AI-first campaign wizard prefills the create form with a drafted
`Partial<CampaignFormState>`. The form's state lives in `useCampaignForm`, which
does `useState(seed ?? { ...INITIAL_STATE, ...draft })`. A `useState` *initializer*
runs exactly once, on the component's first render, and is ignored on every
subsequent render. So if the same form component is already mounted (showing the
empty Step 1) and you merely flip a prop to hand it the `draft`, the new initial
value is never read — the fields stay blank and the prefill silently vanishes.
This is the same family as #4 (assuming a React value is "live" when it's actually
a one-shot snapshot): there, a sticky mutation flag read as still-changing; here, a
state initializer read as re-applied.

**How to apply:** When state is *seeded from a prop/async value via a `useState`
initializer*, the seed MUST exist on the component's first render — so mount the
seeded component only after the value is ready, behind a branch, rather than
toggling a prop on a persistent instance. Here the wizard renders the review form
in a dedicated `review` branch (`{ step:'review'; draft }`) so `CampaignReview`
(and its `useCampaignForm(undefined, draft)`) first-mounts with the draft in hand;
the compose step is a *different* component, so switching steps remounts rather than
re-props. If you genuinely must update already-mounted state from a changing value,
use a controlled value or a deliberate `useEffect(() => setX(value), [value])` reset
— never expect the initializer to re-run. Test it by asserting a drafted field's
`DisplayValue` is present *after* the step transition, not just that the data
arrived.

## 6. A card's entrance animation held `filter: blur(0)`, trapping the `fixed` RunDrawer inside the card

**Why:** The AIZU rebrand gave every `Card` a `.reveal` entrance animation
(`animation: reveal-in ... both`) whose keyframes ended at `filter: blur(0)`. With
`animation-fill-mode: both`, the card *retains* that end state, and **`filter: blur(0)`
is not `filter: none`** — any non-`none` `filter` (also `transform`, `backdrop-filter`,
`will-change`, `contain`, `perspective`) makes the element a *containing block for
`position: fixed` descendants*. The `RunDrawer` renders inside `CampaignCard`'s card and
used `fixed inset-y-3 right-3` for a viewport-anchored right drawer; once the card became
its containing block, the drawer (and its `inset-0` backdrop) was confined to the card's
box — so it appeared as a floating panel sitting in the campaign grid with the rest of the
page un-dimmed. It read as a broken "modal." It only regressed *after* the rebrand because
no ancestor previously held a transform/filter.

**How to apply:** (1) Any overlay that must be viewport-anchored (drawers, modals,
toasts, popovers using `position: fixed`) MUST render through a portal to `document.body`
(`createPortal`) so no ancestor's containing block can trap it. Don't return overlay
markup inline in the component tree. (2) In keyframes/animations, end at `filter: none`
and `transform: none` — never `blur(0)`/`scale(1)` as a *held* value via `both`/`forwards`
fill-mode, or you leave a permanent containing block + stacking context + GPU layer on
every animated element. (3) When a `fixed` element renders relative to the wrong box,
suspect a `transform`/`filter`/`backdrop-filter`/`will-change`/`contain` on an ancestor
before suspecting the element's own CSS.

## 7. Full-bleed shimmer overlay animated with `translateX` added a page-wide horizontal scrollbar

**Why:** The panel-wide card shimmer (`.shimmer-on::after`) swept across with
`transform: translateX(-130% → 330%)`. A CSS `transform` still contributes to an
element's **scroll-overflow** even when `clip-path`/`border-radius` visually clips the
paint — clip-path clips *painting*, not layout geometry. The app shell's `<main>` uses
`overflow-y-auto`, and when one axis is non-`visible` the browser computes the other axis
to `auto` too, so the off-canvas translated pseudo produced a horizontal scrollbar on
*every* page. The Dashboard masked it by accident (its bento tiles set `overflow-hidden`
for the ripple); the shared `Card` on all other pages intentionally has no
`overflow-hidden` (so it won't clip dropdowns/popovers), so there the overflow leaked out.
Same family as #6: a decorative animation on a shared component had a layout side effect
the author didn't account for.

**How to apply:** For decorative sweeps/shimmers/glints on a full-size overlay, animate
`background-position` on a pinned `inset:0` pseudo-element (the box never moves, so it
cannot expand scroll width) instead of `transform: translateX(...)`. Only use a moving
transform if an ancestor clips overflow on BOTH axes — and on shared cards that means
weighing the clip against hiding legitimate overflow (menus/tooltips). After adding any
full-bleed animation, sanity-check no axis overflowed:
`document.documentElement.scrollWidth === document.documentElement.clientWidth`.
