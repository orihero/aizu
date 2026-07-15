"""Reddit engine — official Data API (OAuth2) loop over seeded subreddits.

Text-first (title + selftext relevance, nested comment-tree matching): no vision,
no engagement, no tired-feed/canary/daytime. The closest sibling of the YouTube and
Telegram engines — deterministic operator-seeded discovery (the subreddit list IS
the relevance filter) over a first-class read API, with a per-org encrypted
credential. Exposes ``run_session``.
"""
