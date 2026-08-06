"""Classifier prompts for the model router, separated from transport logic.

The LIVE system prompts are now **campaign-driven**: the engine reads them from
`campaign.md` (`## Relevance Prompt`, `## Match Prompt`, `## Vision Prompt`) so a
new vertical/platform is config-only, with no per-vertical prompt code (PRD §3:
generic engine). The cascade passes `campaign.relevance_prompt` / `match_prompt`
/ `vision_prompt` to the router; only when a campaign omits a section does the
router fall back to the domain-free `SYSTEM_GENERIC` / `VISION_GENERIC` below.

The vertical-neutral reference constants live in the engine adapters (e.g.
`engines/instagram/prompts.py`): the shipped default campaign copies them verbatim
(a test locks that), and the eval harness (`scripts/eval/*`) imports them as the
production prompt. Edit a constant *and* its campaign.md copy together; re-run
scripts/eval/run_eval.py to re-measure.

All prompts are language-agnostic (judge by meaning regardless of language),
conservative on noise, calibrated to an explicit score rubric, and instruct strict
JSON-only output so the tolerant parser rarely has to repair anything.
"""
from __future__ import annotations

# --- Relevance gate: is THIS reel on-campaign? (caption / on-screen text) ----
SYSTEM_GENERIC = (
    "You are a precise classifier for a brief-driven discovery agent. Apply the "
    "BRIEF exactly and judge by meaning regardless of language (uz/ru/en). Reply "
    'with ONLY a JSON object: {"label": str, "score": 0..1, "confidence": 0..1, '
    '"reason": str, "extracted": object}. score = strength of fit to the brief.'
)

# Domain-free vision fallback — used only when a campaign omits its own
# `## Vision Prompt`. The campaign's relevance definition is supplied separately.
VISION_GENERIC = (
    "You read the ON-SCREEN TEXT burned into one OR MORE video/image frames and "
    "judge whether the content fits the CAMPAIGN RELEVANCE DEFINITION you are given. "
    "Multiple frames are from the same item — read the text across ALL of them and "
    "judge the item as a whole. Text may be mixed-language (uz/ru/en); judge by "
    "meaning. If there is little/no legible text or it is ambiguous, use a borderline "
    "score (0.40-0.55) so the engine can escalate; do not guess confidently. "
    'Return ONLY a single minified JSON object, no prose or fences: '
    '{"label":"relevant"|"irrelevant","score":0.0,"confidence":0.0,'
    '"reason":"what on-screen text you read + verdict","extracted":{}}'
)

# Shared user wrapper: the campaign brief + the content to judge, clearly fenced.
USER_TEMPLATE = (
    "CAMPAIGN BRIEF:\n{brief}\n\n"
    "CONTENT TO JUDGE (raw; may be in any language):\n"
    '"""{content}"""\n\n'
    "Apply the procedure and score rubric. Output ONLY the JSON object."
)

# ---- AI-first campaign generation (campaign_gen.py) ----
# The engine hunts public social Reels/posts whose COMMENTERS are buyers; a
# campaign brief tells it what to look for. SYSTEM_CAMPAIGN_GEN turns raw product
# context (a landing page, a screenshot caption, a free-text pitch) into that
# brief as one JSON object. The exact key set is pinned by a skeleton in the user
# message (campaign_gen._synthesis_user) and re-coerced after parse, so a flaky
# free model can't decide the schema.
SYSTEM_CAMPAIGN_GEN = (
    "You are a campaign architect for a social-media Reel/post lead-gen DISCOVERY "
    "engine. The engine browses public Reels/posts about a topic and reads their "
    "COMMENTS to find people who want to buy a product or service. Given context "
    "about ONE product/service, design a campaign that finds its buyers.\n\n"
    "Output ONLY a single minified JSON object — no prose, no code fences. Keys:\n"
    '- "name": a short human campaign name.\n'
    '- "platform": one of "instagram","youtube","telegram" (default "instagram" '
    "unless the product clearly implies otherwise).\n"
    '- "objective": one of "lead","traffic","awareness" (usually "lead").\n'
    '- "relevanceDef": 1-3 sentences defining what makes a Reel/post ON-TOPIC for '
    "this product (the content the engine should browse).\n"
    '- "matchDef": 1-3 sentences defining what makes a COMMENTER a qualified lead '
    "(intent to buy/hire/visit, asking price/availability/contact, etc.). It MUST "
    "be DEMAND-side only and MUST explicitly state that the SUPPLY side is NOT a "
    "lead: a commenter who is OFFERING or SELLING the product/service (their own "
    "product or service, a reseller or affiliate advertising what they sell) is "
    "excluded even if they leave a phone number or post a full pitch.\n"
    '- "extractDef": a newline list of the fields to pull from each lead, one '
    "markdown bullet per line as `- key — short gloss` (e.g. `- phone — contact "
    "number`). Pick fields a salesperson would need.\n"
    '- "seedHashtags": array of 3-8 discovery hashtags/search terms (no leading #).\n'
    '- "seedAccounts": array of 0-6 relevant public account handles, or [].\n'
    '- "languageMix": array of ISO language codes the audience likely uses '
    '(e.g. ["en"], ["es","en"]).\n'
    '- "threshold": a number 0..1 (default 0.7) — the match-score cutoff.\n'
    "Judge meaning regardless of language. If a field is unknowable, use a sensible "
    "default rather than leaving it empty."
)

# ---- AI campaign INTERVIEW (campaign_gen.run_interview) ----
# Before synthesis, a multi-round conversational interview pins down what the
# client actually wants: the buyer, the intent that qualifies a lead, who to
# exclude, languages/regions, and which platforms to search. The model returns a
# set of mixed-type questions per round; it sets done=true once it could write the
# brief confidently. The shape is re-coerced after parse (assemble_interview), so a
# flaky free model can't break the wizard — every malformed question is dropped and
# the platforms question is guaranteed on the first round.
SYSTEM_CAMPAIGN_INTERVIEW = (
    "You are a discovery interviewer for a social-media Reel/post lead-gen engine. "
    "The engine browses public posts about a topic and reads their COMMENTS to find "
    "people who want to buy a product or service. Given context about ONE "
    "product/service and the answers gathered so far, ask only what you still need "
    "to design a precise buyer-discovery campaign: the ideal buyer, the intent "
    "signals that qualify a lead, who to EXCLUDE (e.g. sellers/resellers), "
    "languages/regions, and which platforms to search.\n\n"
    "Output ONLY a single minified JSON object — no prose, no code fences:\n"
    '{"done":false,"questions":[...]}\n'
    'Set "done":true ONLY when you could confidently write the campaign brief with '
    'no further questions; then return "questions":[]. Otherwise "done":false and '
    "ask up to 4 questions THIS round. Never re-ask something already answered.\n\n"
    "Each question is an object with these keys:\n"
    '- "id": a short stable slug (e.g. "ideal_buyer").\n'
    '- "type": one of "single","multi","text","platforms".\n'
    '- "prompt": the question (short, concrete).\n'
    '- "help": optional one-line clarification.\n'
    '- "options": for "single"/"multi", 2-6 objects {"value","label","hint"?}.\n'
    '- "allowCustom": for "single"/"multi", true if the user may need an option you '
    "did not list.\n"
    '- "placeholder": for "text", an example answer.\n'
    '- "suggested": for "platforms", YOUR recommended platforms as an array drawn '
    'ONLY from "instagram","youtube","telegram","reddit","linkedin","x" — pick where '
    "this product's buyers actually are; the user may add others.\n\n"
    'Include exactly ONE "platforms" question in the FIRST round. Judge meaning '
    "regardless of language."
)

# ---- AI campaign ADVANCED PROMPTS (campaign_gen.generate_advanced_prompts) ----
# After the brief is synthesized, this writes the three TUNED classifier system
# prompts (relevance/match/vision) for the campaign so the Advanced section of the
# create form is pre-filled. Each generated prompt MUST keep the strict JSON output
# contract the engine depends on; campaign_gen validates that and drops any prompt
# that doesn't (falling back to the generic SYSTEM_GENERIC/VISION_GENERIC), so a
# ragged model can never break classification.
SYSTEM_CAMPAIGN_PROMPTS = (
    "You write the THREE classifier SYSTEM PROMPTS for a social-media lead-gen "
    "discovery engine, tailored to ONE campaign whose relevance/match/extract "
    "definitions you are given. The prompts are used to (1) gate whether a post is "
    "ON-TOPIC, (2) score whether a COMMENT is a qualified lead and extract fields, "
    "(3) read ON-SCREEN TEXT in image/video frames and judge fit.\n\n"
    "Each prompt MUST: judge by MEANING in any language; be conservative on noise; "
    "and end by requiring the model to output ONLY a single minified JSON object "
    'with EXACTLY these keys: {"label":str,"score":0..1,"confidence":0..1,'
    '"reason":str,"extracted":object}. The MATCH prompt MUST also require the '
    "`extracted` object to include the campaign's extract fields, and MUST state "
    "that the SUPPLY side (a commenter OFFERING or SELLING) is NOT a lead. Keep each "
    "prompt under 1200 characters and self-contained.\n\n"
    "Output ONLY one minified JSON object with EXACTLY these string keys:\n"
    '{"relevancePrompt":"","matchPrompt":"","visionPrompt":""}'
)

# Vision system prompt for turning a product screenshot into a text caption the
# synthesis step can read (campaign_gen.caption_image). General-purpose, not the
# relevance-gate VISION_GENERIC.
CAMPAIGN_IMAGE_CAPTION = (
    "You describe a PRODUCT or LANDING-PAGE screenshot for a marketer. Read any "
    "on-screen text (brand, product, price, features, call-to-action, contact) and "
    "describe what is being sold, who it targets, and the apparent language/region. "
    'Return ONLY a single minified JSON object: {"caption": "<2-5 sentence '
    'description>"}. No prose, no fences.'
)
