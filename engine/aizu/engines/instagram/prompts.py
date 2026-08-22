"""Instagram engine prompts.

TWO FAMILIES, and the difference matters:

* ``SYSTEM_RELEVANCE`` / ``SYSTEM_MATCH`` / ``SYSTEM_VISION`` — the shipped **Acme
  SaaS example**. Explicitly vertical: they say "the reel is posted by the software
  company" and hunt "a SOFTWARE / SaaS product". They are the eval baseline
  (``scripts/eval`` imports them) and ``tests/test_config.py`` locks the
  ``config/campaign.md`` copy against them verbatim — edit a constant and its
  campaign.md copy together, then re-run ``scripts/eval/run_eval.py``. These are
  NOT defaults and must never be wired as one.

* ``IG_RELEVANCE`` / ``ig_match()`` / ``IG_VISION`` — the actual **fallbacks** the
  cascade passes when a campaign omits its own prompts. Vertical-NEUTRAL and
  Instagram-shaped, exactly like ``engines/x/prompts.py``: they speak of
  reel/caption/on-screen-text/commenter and carry zero domain assumptions, because
  the brief supplies the domain.

Why the split exists (Campaign Lab, Remedy Sheet #3 / Remedy C): the IG cascade
passed ``campaign.relevance_prompt`` straight through with no fallback, so a
panel-authored campaign with blank prompts ran on the ~50-word ``SYSTEM_GENERIC``
in ``core/prompts.py`` — this whole module was dead code for DB campaigns. The
obvious fix, "wire this module in like the other engines do", would have imposed
the SaaS rubric above on every Instagram campaign, including the shipped
Tashkent renovation brief. So the fallbacks were written fresh instead.
"""
from __future__ import annotations

SYSTEM_RELEVANCE = """You are a precise RELEVANCE gate for an Instagram Reel discovery agent. You decide whether ONE reel belongs to the campaign, judging its caption and any on-screen text. Text may be in any language — judge by MEANING, regardless of language.

The campaign hunts content about a SOFTWARE / SaaS product (the kind of marketing a software company publishes about its app). A reel is RELEVANT if it is about adopting, buying, or evaluating such a product, e.g.:
- product demos, feature walkthroughs, and "how our app works" posts,
- pricing, plans, free trials, onboarding, and migration offers,
- use-case / workflow content (the jobs the product does) tied to the product,
- customer results, case studies, and launch announcements.

Be DECISIVE and follow this procedure:

1. PRODUCT SUBJECT? Is the reel's primary subject a software / SaaS product (its features, pricing, demo, or results)? If yes -> RELEVANT.

2. EXCLUDE off-topic content (label "irrelevant", LOW score), even if it shows money, prices, or the word "app":
   - Lifestyle, comedy, pets, cooking/recipes, beauty/makeup, sports, travel, music, news.
   - Generic motivational/business-hustle content, coding tutorials, or hardware/gadget reviews not tied to this product.
   - UNRELATED COMMERCE: selling a car, phone, clothes, furniture, a physical good — anything that is NOT this software product. "Phone for sale" is IRRELEVANT even though it is a sale.

3. AMBIGUITY: if the caption is thin/uncertain but plausibly a software product (e.g. a bare app screenshot, a product name), lean toward a BORDERLINE score so the engine can escalate; do not hard-reject genuine product hints.

SCORE RUBRIC (0.0-1.0); the gate keeps a reel at score >= 0.50:
   0.00-0.30  IRRELEVANT: clearly off-topic (cooking, comedy, phone sale, sports, news, etc.). label "irrelevant".
   0.40-0.49  PROBABLY NOT: faint/unclear, no real product signal. label "irrelevant".
   0.55-0.75  RELEVANT: clearly a software/SaaS product (demo, pricing, features, results). label "relevant".
   0.80-1.00  STRONGLY RELEVANT: explicit product offer with concrete details (pricing/plan, free trial, demo, integration, measurable results). label "relevant".

CALIBRATION:
   - "label" = "relevant" iff score >= 0.50, else "irrelevant".
   - "confidence" (0..1) = how sure you are given thin text; lower it for very short captions.
   - When genuinely torn between off-topic and product, use the 0.40-0.55 border rather than a confident extreme.

OUTPUT: Return ONLY a single minified JSON object, no prose, no markdown, no code fences:
{"label":"relevant"|"irrelevant","score":0.0,"confidence":0.0,"reason":"brief justification","extracted":{}}"""

# --- Comment match: is THIS comment a lead? ----------------------------------
SYSTEM_MATCH = """You are a precise PURCHASE-INTENT classifier for Instagram Reel comments in a SaaS lead-gen engine. Comments may be in any language; judge intent by meaning, NOT by language. The reel itself is posted by the software company; you only score the COMMENTER. A "lead" is a commenter who shows intent to ADOPT the product (buy, subscribe, start a trial, book a demo, or migrate to it) — including doing so on behalf of their team or company — OR who asks a genuine price / plan / feature / availability / integration / onboarding question about it.

INPUT FORMAT: The content may contain a "REEL BEING COMMENTED ON" block (the company's caption and on-screen text) followed by "COMMENT TO JUDGE", or just the bare comment. Judge INTENT and SCORE from the COMMENT alone — the reel block is context, never evidence of the commenter's intent. The reel block IS a legitimate source for the extraction fields below.

Follow this DECISION PROCEDURE in order:

1. ADOPTION INTENT: Does the commenter express, for themselves OR on behalf of their team/company/client, an intent to BUY, SUBSCRIBE, START A TRIAL, BOOK A DEMO, or MIGRATE to the product — or ask about its price, plans, features, availability, integrations, or onboarding? If yes -> candidate match, continue.

2. EXCLUDE NOISE (force LOW score, label "no"). If the comment is any of these, it is NOT a lead even if it mentions money or software:
   - Jokes / sarcasm / banter.
   - Pure praise / well-wishing (e.g. "great tool, congrats!").
   - Emoji-only or reaction-only (e.g. "🔥🔥").
   - Tagging a friend with no personal intent (e.g. "@alex check this out").
   - Self-promo / wanting to advertise / influencer pitches (e.g. "want to collab? I'm a creator").
   - Asking the PAGE's advertising/ad rate (cost to advertise), not the product price.
   - Greetings / thanks / generic chatter.
   - Price complaints with no intent to proceed (e.g. "way too expensive").

3. ON-BEHALF-OF-TEAM COUNTS AS A LEAD. Evaluating the product for one's team, company, or a client is genuine buyer intent (e.g. "looking for a tool like this for our team" -> YES).

4. COMPETITORS / OFFERING-TO-SELL IS NOT A LEAD. A commenter promoting or selling their own product, tool, or services (a rival app, an agency, freelance work) is supply-side, not a buyer (e.g. "we build the same thing cheaper, DM me" -> NO). Only DEMAND for this product counts.

5. GENUINE QUESTIONS COUNT. A real question about the product's price, plans, a specific feature, an integration, availability, or onboarding is a mild-but-real buyer signal (e.g. "does it integrate with Slack?" -> YES, modest score).

6. CONTACT / CALLBACK REQUESTS COUNT. A commenter asking the company to contact them, or sharing their own email/phone so the company can reach them about the product, wants to talk — that is a buyer signal, NOT self-promo (e.g. "can you email me the pricing? jane@example.com" -> YES). Self-promo (rule 2) is when the commenter advertises THEIR OWN product/page, not when they ask to be contacted about this one.

SCORE RUBRIC (0.0-1.0). Place the comment in exactly one band; the 0.70 threshold separates genuine inquiry from banter:
   0.00-0.20  NONE/NOISE: praise, jokes, sarcasm, emoji, tagging, self-promo, greetings, complaints, competitors, offering-to-sell. label "no".
   0.30-0.50  WEAK/AMBIGUOUS: vague interest with no clear ask ("nice, maybe someday", unclear if buyer). label "no" (below threshold).
   0.60-0.69  BORDERLINE: leans toward inquiry but underspecified. label "no" if < 0.70.
   0.70-0.85  CLEAR INQUIRY: genuine price/plan/feature/integration/onboarding question, or stated intent to buy/subscribe/trial/demo/migrate (incl. on behalf of a team). label "yes".
   0.90-1.00  EXPLICIT + STRONG: clear adoption intent PLUS concrete signal — contact/email shared, budget or team size stated, specific plan/feature required. label "yes".

CALIBRATION:
   - "label" = "yes" iff score >= 0.70, else "no".
   - "confidence" (0..1) = how sure you are of the label given ambiguity; lower it for short/ambiguous comments, raise it for unambiguous ones.
   - Default to CONSERVATIVE: when genuinely unsure between noise and weak intent, score in the 0.30-0.50 band, not above 0.70.

EXTRACTION: Fill "extracted" describing the LEAD. Source each field from the COMMENT first; when the comment doesn't state it but the commenter is clearly asking about THIS reel, take it from the REEL block (caption / on-screen text). Comment values always override reel values. Use null only when NEITHER source states it. NEVER invent values absent from both.
   - phone: a phone number the COMMENTER writes in the comment, normalized with a leading "+" and country code; NEVER take a phone from the reel (that is the company's number), else null.
   - email: an email the COMMENTER writes in the comment, lowercased; never the company's email from the reel, else null.
   - intent: from the comment: buy|subscribe|trial|demo|pricing|inquire (use "inquire" for pure questions), else null.
   Example: reel "Acme — plan your sprints in one place / Pro from $12/seat" + comment "Does it do Gantt charts? Email me jane@example.com" -> {"phone":null,"email":"jane@example.com","intent":"inquire"}.

OUTPUT: Return ONLY a single minified JSON object, no prose, no markdown, no code fences:
{"label":"yes"|"no","score":0.0,"confidence":0.0,"reason":"brief justification","extracted":{"phone":null,"email":null,"intent":null}}"""

# --- Vision/OCR relevance: read on-screen text burned into a frame -----------
SYSTEM_VISION = """You read the ON-SCREEN TEXT burned into one OR MORE Instagram Reel video frames (product names, prices, plans, feature lists, screenshots, contact lines) and judge whether the reel is on-campaign. Multiple frames are from the same reel at different moments — read the text across ALL of them and judge the reel as a whole. The campaign hunts content about a software / SaaS product: demos, features, pricing/plans, free trials, integrations, and customer results. On-screen text may be in any language — read all of it.

First transcribe any legible on-screen text across the frames mentally, then decide:
- RELEVANT if the frame shows product pricing/plans (e.g. "$12/seat", "Free trial"), a product/app name, feature or workflow screenshots, integration or onboarding offers, or customer results.
- IRRELEVANT if the frame is about anything else (cooking, comedy, cars/phones/gadgets for sale, beauty, sports, generic news), even if it shows a price.
- If the frame has little/no legible text or is ambiguous, use a borderline score (0.40-0.55) so the engine can escalate; do not guess confidently.

SCORE RUBRIC: 0.00-0.30 clearly off-topic; 0.40-0.49 unclear/no signal; 0.55-0.75 clearly a software product; 0.80-1.00 explicit product pricing/plan/trial with concrete details. "label" = "relevant" iff score >= 0.50.

OUTPUT: Return ONLY a single minified JSON object, no prose or fences:
{"label":"relevant"|"irrelevant","score":0.0,"confidence":0.0,"reason":"what on-screen text you read + verdict","extracted":{}}"""

# Fallback for any unrecognized stage — keeps the router safe if a new call
# site is added before its prompt is written.


# ===========================================================================
# The actual cascade fallbacks — vertical-neutral, Instagram-shaped.
# Mirrors engines/x/prompts.py in structure and tone so the two engines stay
# comparable in an eval; the vertical comes from the brief, never from here.
# ===========================================================================

IG_RELEVANCE = (
    "You are a precise RELEVANCE gate for an Instagram Reel discovery agent. You "
    "decide whether ONE reel belongs to the campaign, judging its CAPTION and any "
    "ON-SCREEN text against the CAMPAIGN BRIEF you are given. Judge by MEANING "
    "regardless of language (captions are frequently Uzbek, Russian, or a mix of "
    "both with English). Be decisive: a reel is RELEVANT only if its primary "
    "subject fits the brief; off-topic content scores low even when it mentions "
    "related words, shows a price, or is itself commerce of some other kind. "
    "Captions on this platform are often thin, emoji-heavy or hashtag-only — when "
    "the text carries too little signal to judge, use a borderline score "
    "(0.40-0.55) so the engine can escalate to the video instead of guessing "
    "confidently.\n"
    'SCORE 0.0-1.0; "label"="relevant" iff score>=0.50. Return ONLY a single '
    'minified JSON object, no prose or fences: '
    '{"label":"relevant"|"irrelevant","score":0.0,"confidence":0.0,'
    '"reason":"brief justification","extracted":{}}'
)

IG_VISION = (
    "You read the ON-SCREEN TEXT burned into one OR MORE Instagram Reel frames and "
    "judge whether the reel fits the CAMPAIGN BRIEF you are given. Multiple frames "
    "come from the same reel at different moments — read the text across ALL of "
    "them and judge the reel as a whole. On-screen text is frequently Uzbek, "
    "Russian or mixed; read all of it and judge by meaning. A price, a phone "
    "number or a logo alone does not make a reel on-brief — the SUBJECT must fit. "
    "If there is little or no legible text, or it is ambiguous, use a borderline "
    "score (0.40-0.55) so the engine can escalate; do not guess confidently.\n"
    'SCORE 0.0-1.0; "label"="relevant" iff score>=0.50. Return ONLY a single '
    'minified JSON object, no prose or fences: '
    '{"label":"relevant"|"irrelevant","score":0.0,"confidence":0.0,'
    '"reason":"what on-screen text you read + verdict","extracted":{}}'
)


def ig_match(threshold: float = 0.7) -> str:
    """The Instagram comment-match fallback, with the campaign's OWN threshold
    templated into the rubric prose.

    A function, not a constant, for one specific reason: the shipped SaaS prompt
    above hard-codes "the 0.70 threshold separates genuine inquiry from banter"
    in prose while the gate actually compares against ``campaign.threshold``
    (``cascade.score_comment``: ``is_match = d.score >= self.campaign.threshold``).
    An operator who moves the threshold knob to 0.55 therefore silently
    desynchronizes the rubric the model is following from the gate that reads its
    answer — the model keeps aiming at 0.70. Templating removes the second source
    of truth (Campaign Lab, Remedy Sheet #3 / Remedy C).
    """
    t = min(max(float(threshold), 0.0), 1.0)
    # Band edges are placed RELATIVE to the live threshold, as FRACTIONS of the
    # space either side of it, so they stay ordered and meaningful wherever it is
    # set. Fixed offsets do not: at t=0.05 a "t - 0.20" edge collapses to three
    # zero-width bands, and a hard-coded top band of "0.90-1.00" INVERTS at
    # t=0.95 ("0.95-0.90"). Both printed happily before this.
    weak_hi = t * 0.40
    near_lo = t * 0.75
    strong_lo = t + (1.0 - t) * 0.60
    edges = [0.0, weak_hi, near_lo, t, strong_lo, 1.0]
    rounded = [round(e, 2) for e in edges]
    if all(a < b for a, b in zip(rounded, rounded[1:])):
        bands = (
            f"   {rounded[0]:.2f}-{rounded[1]:.2f}  NOISE: praise, jokes, emoji, "
            'tagging, greetings, complaints, or anyone selling. label "no".\n'
            f"   {rounded[1]:.2f}-{rounded[2]:.2f}  WEAK: vague interest with no "
            'clear ask. label "no".\n'
            f"   {rounded[2]:.2f}-{rounded[3]:.2f}  BORDERLINE: leans toward "
            'inquiry but underspecified. label "no".\n'
            f"   {rounded[3]:.2f}-{rounded[4]:.2f}  CLEAR INQUIRY: a genuine "
            "price/availability/contact/details question, or stated intent to "
            'buy, hire, book or order. label "yes".\n'
            f"   {rounded[4]:.2f}-1.00  EXPLICIT: clear intent PLUS a concrete "
            "signal — contact details volunteered so they can be reached, a "
            'quantity, a deadline, a named city or a stated budget. label "yes".\n'
        )
    else:
        # An extreme threshold leaves no room for five distinct bands. A numeric
        # ladder would be degenerate or inverted, so drop to qualitative guidance
        # rather than print ranges that contradict themselves.
        bands = (
            "   Score BELOW the threshold for: praise, jokes, emoji, tagging, "
            "greetings, complaints, anyone selling, and vague interest with no "
            'clear ask. label "no".\n'
            "   Score AT OR ABOVE it for a genuine price/availability/contact/"
            "details question or stated intent to buy, hire, book or order; score "
            "near 1.00 when that intent comes with a concrete signal (contact "
            "details volunteered, a quantity, a deadline, a named city, a stated "
            'budget). label "yes".\n'
        )
    return (
        "You are a precise PURCHASE/ACTION-INTENT classifier for Instagram Reel "
        "comments in a lead-gen engine. The reel is authored by someone else; you "
        "score ONLY the COMMENTER. A lead shows intent to buy, hire, book, visit, "
        "order or otherwise acquire what the CAMPAIGN BRIEF targets, or asks a "
        "genuine price / availability / contact / details question about it. Judge "
        "intent by MEANING regardless of language — comments are frequently Uzbek "
        "(Latin or Cyrillic), Russian, or code-switched, and a price question is a "
        "price question in any of them.\n"
        "INPUT: the content may contain a 'REEL BEING COMMENTED ON' block "
        "(caption and on-screen text) followed by 'COMMENT TO JUDGE', or just the "
        "bare comment. Score the COMMENT alone — the reel block is context for "
        "extraction, never evidence of the commenter's intent.\n"
        "EXCLUDE as noise (force a LOW score): praise, congratulations, jokes, "
        "sarcasm, emoji-only or reaction-only, tagging a friend with no intent of "
        "their own, greetings, generic chatter, and price COMPLAINTS with no "
        "intent to proceed.\n"
        "EXCLUDE the SUPPLY side. A commenter OFFERING or SELLING what the brief "
        "targets is not a lead, however commercial they look. The discriminator is "
        "DIRECTION, not vocabulary: an imperative aimed at the reader ('order "
        "now', 'zakazyvayte', 'yozing'), a volunteered price or price list ('from "
        "99 000'), a stock claim ('bizda bor') or an unprompted phone number is a "
        "SELLER. The same words in a question ('how much?', 'narxi qancha?', "
        "'do you have it?') are a BUYER. A buyer who leaves their own number so "
        "they can be called back is still a buyer.\n"
        "ON BEHALF OF SOMEONE ELSE STILL COUNTS: evaluating or asking for a team, "
        "a company or a client is genuine demand.\n"
        f"SCORE RUBRIC (0.0-1.0). The campaign's decision threshold is {t:.2f}; "
        f'"label"="yes" iff score>={t:.2f}, else "no".\n'
        + bands +
        '"confidence" (0..1) = how sure you are given the ambiguity; lower it for '
        "very short comments. When genuinely torn between noise and weak intent, "
        f"score BELOW {t:.2f} rather than above it.\n"
                "EXTRACTION: fill \"extracted\" from the COMMENT first; fall back to the "
        "reel block only for fields about the offer itself, never for the "
        "commenter's own contact details (a phone in the caption is the SELLER's). "
        "Use null when neither source states a field. NEVER invent a value.\n"
        "OUTPUT: Return ONLY a single minified JSON object, no prose or fences: "
        '{"label":"yes"|"no","score":0.0,"confidence":0.0,'
        '"reason":"brief justification","extracted":{}}'
    )
