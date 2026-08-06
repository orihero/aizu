# Mockups

Standalone, dependency-light HTML/CSS/JS prototypes built **before** the admin-panel rebuild.
Frozen historical reference — not maintained, not part of the build.

- `chart-lab/` — hand-rolled SVG vs. Recharts trade-off study (has its own README).
- `admin-pulse/` — an overview page applying the approach that won in `chart-lab`.
- `pulse-app/` — a full six-page product prototype: index, campaigns, campaign-new, leads,
  reports, settings.

Their design tokens are the origin of the shipped "Ink × Lime" system in
`admin-panel/src/index.css` — useful provenance if you're wondering why the palette looks the
way it does, but not a style guide to design new screens against. For new work use
`admin-panel/src/index.css` and the shipped components, and treat everything here as
read-only history.
