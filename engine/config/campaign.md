# campaign.md — lead-gen (Acme, a generic SaaS product)

All domain meaning lives here. Swap this file and the same binary runs a
different hunt with zero code change. Read once at session start.

This is the SHIPPED DEFAULT / reference campaign: a deliberately vertical-neutral
example that hunts buyers for a generic SaaS product ("Acme"). It is here to
demonstrate the shape, not to lock the engine to software — replace it (or point
the engine at another `config/campaigns/*.md`) to hunt any product, good, or
service.

The `## Relevance Prompt`, `## Match Prompt`, and `## Vision Prompt` sections at
the bottom are the **system prompts** the model router uses per stage — the
vertical lives in config, not code (PRD §3). Retune them here; remove a section
to fall back to a domain-free generic prompt. The short `## Relevance` / `##
Match` / `## Extract` sections above stay the human-readable brief.

```yaml
campaign_id: acme-saas-leadgen
goal: lead
language_mix: [en]
threshold: 0.70          # comment score >= threshold => match
escalate_band: [0.40, 0.75]   # local confidence inside this band => ask cloud

# Active discovery — works on any account, not just a pre-steered home feed.
# EXAMPLE seeds — tailor these to your own product, niche, and language region.
seed_hashtags: [projectmanagement, productivitytools, saas, teamcollaboration, taskmanagement, remotework]
seed_accounts: [acme.io]

# Engagement is OFF for harvest: writes (likes/follows) belong to the warming
# engine, not lead discovery (warming PRD C1 / §4.3 — harvest stays read-only).
enable_actions: false
max_likes_per_session: 8       # (warming-only caps; ignored while harvesting)
max_follows_per_session: 4

# Local Uzbek STT ("KotibAI") third relevance-gate tier, Instagram-only.
# requires language_mix to include "uz" AND AIZU_STT_ENABLED=true
enable_stt: false
```

## Seed / feed direction (manual warming + mobile re-steer)
Software / SaaS product content: app demos, feature walkthroughs, pricing and
plans, free trials, onboarding and migration, workflow and use-case posts, and
the marketing this company publishes about its product. The example seeds in the
YAML above are starting points — tailor hashtags and accounts to your own market.

## Relevance (does this reel belong to the hunt?)
A reel is relevant if its caption **or on-screen text** is about adopting,
buying, or evaluating a software / SaaS product.

Relevant: product demos and feature walkthroughs, pricing / plans / free-trial
offers, onboarding and migration, workflow and use-case content tied to the
product, customer results, case studies, and launch announcements.

Not relevant — score low even if money or the word "sale" appears: generic
lifestyle, comedy, cooking, beauty, sports, travel, gadgets, news, general
finance/markets, and **unrelated commerce** (selling a car, phone, furniture,
clothes — anything that is not this software product).

## Match (is this comment a lead?)
A comment is a match if the COMMENTER signals demand for the product — intent to
buy, subscribe, start a trial, book a demo, or migrate to it, or a genuine
question about price, plans, a feature, an integration, availability, or
onboarding — **for themselves, their team, or on someone's behalf** (e.g.
evaluating it for their company).

Not a match: pure praise or congratulations, jokes / sarcasm / banter,
emoji-only reactions, tagging a friend, self-promo or influencer/ad pitches,
asking the page's advertising rate, greetings, price complaints with no intent,
and **anyone offering to sell something** (a rival tool, an agency, freelance
services) — a competitor or seller is supply-side, not a buyer lead.

## Extract (the brief-defined `extracted` JSON)
- `phone` — any phone number in the comment, normalized (+country code), else null.
- `email` — any email address in the comment, lowercased, else null.
- `intent` — short tag: buy | subscribe | trial | demo | pricing | inquire.

## Relevance Prompt
You are a precise RELEVANCE gate for an Instagram Reel discovery agent. You decide whether ONE reel belongs to the campaign, judging its caption and any on-screen text. Text may be in any language — judge by MEANING, regardless of language.

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
{"label":"relevant"|"irrelevant","score":0.0,"confidence":0.0,"reason":"brief justification","extracted":{}}

## Match Prompt
You are a precise PURCHASE-INTENT classifier for Instagram Reel comments in a SaaS lead-gen engine. Comments may be in any language; judge intent by meaning, NOT by language. The reel itself is posted by the software company; you only score the COMMENTER. A "lead" is a commenter who shows intent to ADOPT the product (buy, subscribe, start a trial, book a demo, or migrate to it) — including doing so on behalf of their team or company — OR who asks a genuine price / plan / feature / availability / integration / onboarding question about it.

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
{"label":"yes"|"no","score":0.0,"confidence":0.0,"reason":"brief justification","extracted":{"phone":null,"email":null,"intent":null}}

## Vision Prompt
You read the ON-SCREEN TEXT burned into one OR MORE Instagram Reel video frames (product names, prices, plans, feature lists, screenshots, contact lines) and judge whether the reel is on-campaign. Multiple frames are from the same reel at different moments — read the text across ALL of them and judge the reel as a whole. The campaign hunts content about a software / SaaS product: demos, features, pricing/plans, free trials, integrations, and customer results. On-screen text may be in any language — read all of it.

First transcribe any legible on-screen text across the frames mentally, then decide:
- RELEVANT if the frame shows product pricing/plans (e.g. "$12/seat", "Free trial"), a product/app name, feature or workflow screenshots, integration or onboarding offers, or customer results.
- IRRELEVANT if the frame is about anything else (cooking, comedy, cars/phones/gadgets for sale, beauty, sports, generic news), even if it shows a price.
- If the frame has little/no legible text or is ambiguous, use a borderline score (0.40-0.55) so the engine can escalate; do not guess confidently.

SCORE RUBRIC: 0.00-0.30 clearly off-topic; 0.40-0.49 unclear/no signal; 0.55-0.75 clearly a software product; 0.80-1.00 explicit product pricing/plan/trial with concrete details. "label" = "relevant" iff score >= 0.50.

OUTPUT: Return ONLY a single minified JSON object, no prose or fences:
{"label":"relevant"|"irrelevant","score":0.0,"confidence":0.0,"reason":"what on-screen text you read + verdict","extracted":{}}
