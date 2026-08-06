"""X (Twitter) engine (PRD: docs/prd/x-lead-agent-PRD.md).

Managed-CDP, exactly like Instagram: attach to a warmed, logged-in Chrome over
``connect_over_cdp`` and read x.com's own internal GraphQL traffic read-only — no
per-org secret, no connect card. Discovery walks the For You feed + Search + Lists;
the post's text is the primary surface, with the vision/OCR tier reading on-screen
text only for image/video posts.

X has TWO match surfaces, unlike its siblings: threaded **replies** and standalone
**quote-posts**. ``XFeed.fetch_comments`` merges both internal GraphQL sources
behind the one FeedSource interface, paged by a composite cursor in the single
cursor slot (PRD §5, §7). Read-only: never likes, reposts, follows, replies,
quotes, bookmarks, or DMs. Exposes ``run_session``.

X is the most automation-hostile platform of the family: it enforces hard daily
read-view caps and rotates GraphQL ``doc_id``s every ~2–4 weeks, so the session
carries a read-budget soft-flag and halts hard on the empty-interception canary
and on any Arkose/checkpoint challenge.
"""
