"""reelradar.engines — one self-contained engine per social network.

Each engine (``instagram``, ``youtube``, ``telegram``) owns its own
orchestration loop, content model, access adapter, pacing/engagement/halt
policy, and scoring (relevance->match->extract) + prompts. Engines depend on
``reelradar.core`` for shared transport (Router), persistence (Store), brief
parsing (Campaign), and the FeedSource content interface — never on each other.

``reelradar.dispatch`` selects the engine for a run by ``campaign.platform`` and
calls its module-level ``run_session(...)`` (see ``base.EngineProtocol``).
"""
