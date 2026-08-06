# UI Mistakes Log

## 1. Dead navigation link left behind after an IA refactor

**Why:** When the Pulse rebuild collapsed the old 8-page IA into 5 pages, the standalone Health page was removed and `/health` was turned into a legacy redirect to `/dashboard`. But the `HaltBanner`'s "View health" button still pointed at `/health`, so it silently dumped the user on the Dashboard — a page with no health indicators at all. The health UI had actually moved into the Reports page (`SystemHealthTile`), but nothing repointed the link there. Root cause: treating route removal and the links/redirects that target it as separate concerns instead of updating them together.

**How to apply:** When deleting or repurposing a route, grep for every `to="/<route>"`, `<Navigate to>`, and `path: '/<route>'` referencing it and repoint them to the real destination in the same change. A redirect that lands on an unrelated page is worse than a 404 — it looks like success. After an IA change, click every CTA/banner/button that deep-links and confirm it lands on a page that actually shows the promised content. Keep deep-link targets in a shared constant (e.g. `healthAnchor.ts`) so the source (banner) and destination (page anchor) can't drift apart.
