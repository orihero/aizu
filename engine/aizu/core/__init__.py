"""aizu.core — platform-neutral kernel shared by every per-platform engine.

These modules carry zero platform knowledge: the LLM transport (``router``), the
DB (``store``), brief parsing (``config``), the FeedSource/Reel/Comment content
interface (``feed``), pacing primitives (``pacing``), JSON parsers, logging, and
the generic prompt fallbacks (``prompts``). Per-platform engines
(``aizu.engines.*``) depend on core; core never imports an engine.
"""
