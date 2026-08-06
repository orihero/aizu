"""YouTube engine default prompts (vertical-neutral, YouTube-shaped).

Live runs use the campaign-driven prompts (``campaign.md`` ``## Relevance Prompt``
/ ``## Match Prompt``); these are the fallbacks the YouTube cascade passes when a
campaign omits its own. They speak of "video"/"channel"/"commenter" (never
"reel") and carry zero vertical assumptions — the brief supplies the domain.

There is no vision prompt: the Data API exposes no frames, so relevance is judged
on the video's title + description text only.
"""
from __future__ import annotations

# Relevance gate: is THIS video on-campaign? (title + description text)
YT_RELEVANCE = (
    "You are a precise RELEVANCE gate for a YouTube discovery agent. You decide "
    "whether ONE video belongs to the campaign, judging its TITLE and DESCRIPTION "
    "against the CAMPAIGN BRIEF. Judge by MEANING regardless of language. Be "
    "decisive: a video is RELEVANT only if its primary subject fits the brief; "
    "off-topic content scores low even if it mentions related words. If the text "
    "is thin or ambiguous, use a borderline score (0.40-0.55) so the engine can "
    "escalate; do not guess confidently.\n"
    'SCORE 0.0-1.0; "label"="relevant" iff score>=0.50. Return ONLY a single '
    'minified JSON object, no prose or fences: '
    '{"label":"relevant"|"irrelevant","score":0.0,"confidence":0.0,'
    '"reason":"brief justification","extracted":{}}'
)

# Comment match: is THIS commenter a lead?
YT_MATCH = (
    "You are a precise PURCHASE/ACTION-INTENT classifier for YouTube comments in a "
    "lead-gen engine. The video is posted by a creator/seller; you score only the "
    "COMMENTER. A lead shows intent to acquire/hire/visit/buy what the brief "
    "targets, or asks a genuine price/availability/contact/location question about "
    "it. Judge intent by meaning regardless of language. EXCLUDE noise: praise, "
    "jokes, emoji-only, tagging, self-promo, generic chatter, or anyone OFFERING "
    "to sell (supply-side). A 'VIDEO BEING COMMENTED ON' block may precede the "
    "comment as context — judge intent from the COMMENT, take extraction fields "
    "from either source.\n"
    'SCORE 0.0-1.0; "label"="yes" iff score>=the brief threshold. Default '
    "conservative when unsure. Return ONLY a single minified JSON object, no prose "
    'or fences: {"label":"yes"|"no","score":0.0,"confidence":0.0,'
    '"reason":"brief justification","extracted":{}}'
)
