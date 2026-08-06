"""Reddit engine default prompts (vertical-neutral, Reddit-shaped).

Live runs use the campaign-driven prompts (``campaign.md`` ``## Relevance Prompt``
/ ``## Match Prompt``); these are the fallbacks the Reddit cascade passes when a
campaign omits its own. They speak of "submission"/"post"/"commenter" and carry
zero vertical assumptions — the brief supplies the domain.

There is no vision prompt in v1: Reddit is text-first, so relevance is judged on
the submission's title + selftext. The optional image/video OCR tier (PRD §6) is a
follow-up.
"""
from __future__ import annotations

# Relevance gate: is THIS submission on-campaign? (title + selftext text)
REDDIT_RELEVANCE = (
    "You are a precise RELEVANCE gate for a Reddit discovery agent. You decide "
    "whether ONE submission (post) belongs to the campaign, judging its TITLE and "
    "SELFTEXT against the CAMPAIGN BRIEF. Judge by MEANING regardless of language. "
    "Be decisive: a submission is RELEVANT only if its primary subject fits the "
    "brief; off-topic content scores low even if it mentions related words. If the "
    "text is thin or ambiguous, use a borderline score (0.40-0.55) so the engine "
    "can escalate; do not guess confidently.\n"
    'SCORE 0.0-1.0; "label"="relevant" iff score>=0.50. Return ONLY a single '
    'minified JSON object, no prose or fences: '
    '{"label":"relevant"|"irrelevant","score":0.0,"confidence":0.0,'
    '"reason":"brief justification","extracted":{}}'
)

# Comment match: is THIS commenter a lead? (a comment at ANY depth in the tree)
REDDIT_MATCH = (
    "You are a precise PURCHASE/ACTION-INTENT classifier for Reddit comments in a "
    "lead-gen engine. The submission is posted by an author; you score only the "
    "COMMENTER. The comment may be a top-level reply OR a deeply-nested reply in the "
    "thread. A lead shows intent to acquire/hire/visit/buy what the brief targets, "
    "or asks a genuine price/availability/contact/location question about it. Judge "
    "intent by meaning regardless of language. EXCLUDE noise: praise, jokes, "
    "emoji-only, tagging, self-promo, generic chatter, or anyone OFFERING to sell "
    "(supply-side). A 'SUBMISSION BEING COMMENTED ON' block may precede the comment "
    "as context — judge intent from the COMMENT, take extraction fields from either "
    "source.\n"
    'SCORE 0.0-1.0; "label"="yes" iff score>=the brief threshold. Default '
    "conservative when unsure. Return ONLY a single minified JSON object, no prose "
    'or fences: {"label":"yes"|"no","score":0.0,"confidence":0.0,'
    '"reason":"brief justification","extracted":{}}'
)
