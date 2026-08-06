"""LinkedIn engine (PRD: docs/prd/linkedin-lead-agent-PRD.md).

Managed-CDP, exactly like Instagram: attach to a warmed, logged-in Chrome over
``connect_over_cdp`` and read LinkedIn's own internal Voyager JSON traffic
read-only — no per-org secret, no connect card. Discovery walks the home feed plus
seeded people/companies/hashtags; relevance is judged on the post copy first, with
the vision/OCR tier reading carousel/document/image text only when the copy is
thin. A single match surface (post comments). Read-only: never likes, reacts,
follows, connects, comments, or messages. Exposes ``run_session``.

LinkedIn runs the most aggressive automation enforcement of the family, so the
session is the most conservatively paced and halts hard on any checkpoint.
"""
