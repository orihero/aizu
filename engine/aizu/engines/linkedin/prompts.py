"""LinkedIn engine default prompts (vertical-neutral, LinkedIn-shaped).

Live runs use the campaign-driven prompts (``campaign.md`` ``## Relevance Prompt`` /
``## Match Prompt`` / ``## Vision Prompt``); these are the fallbacks the LinkedIn
cascade passes when a campaign omits its own. They speak of "post"/"copy"/
"commenter" and carry zero vertical assumptions — the brief supplies the domain.

LinkedIn is copy-first; the vision prompt reads carousel/document/image text only
when the post copy is thin (PRD §6).
"""
from __future__ import annotations

# Relevance gate: is THIS post on-campaign? (post copy; vision only when thin)
LINKEDIN_RELEVANCE = (
    "You are a precise RELEVANCE gate for a LinkedIn discovery agent. You decide "
    "whether ONE post belongs to the campaign, judging its COPY (and, when shown, "
    "the on-screen text of an attached carousel/document/image) against the "
    "CAMPAIGN BRIEF. Judge by MEANING regardless of language. Be decisive: a post is "
    "RELEVANT only if its primary subject fits the brief; off-topic content scores "
    "low even if it mentions related words. If the copy is thin or ambiguous, use a "
    "borderline score (0.40-0.55) so the engine can escalate; do not guess "
    "confidently.\n"
    'SCORE 0.0-1.0; "label"="relevant" iff score>=0.50. Return ONLY a single '
    'minified JSON object, no prose or fences: '
    '{"label":"relevant"|"irrelevant","score":0.0,"confidence":0.0,'
    '"reason":"brief justification","extracted":{}}'
)

# Comment match: is THIS commenter a lead? (a comment on a relevant post)
LINKEDIN_MATCH = (
    "You are a precise PURCHASE/ACTION-INTENT classifier for LinkedIn comments in a "
    "lead-gen engine. The post is authored by someone; you score only the COMMENTER. "
    "A lead shows intent to acquire/hire/visit/buy/partner on what the brief targets, "
    "or asks a genuine price/availability/contact/details question about it. Judge "
    "intent by meaning regardless of language. EXCLUDE noise: congratulations, "
    "praise, emoji-only, tagging colleagues, self-promo, generic chatter, or anyone "
    "OFFERING to sell (supply-side). A 'POST BEING COMMENTED ON' block may precede "
    "the comment as context — judge intent from the COMMENT, take extraction fields "
    "from either source.\n"
    'SCORE 0.0-1.0; "label"="yes" iff score>=the brief threshold. Default '
    "conservative when unsure. Return ONLY a single minified JSON object, no prose "
    'or fences: {"label":"yes"|"no","score":0.0,"confidence":0.0,'
    '"reason":"brief justification","extracted":{}}'
)
