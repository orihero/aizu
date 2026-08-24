"""Campaign Lab — turn guessed campaign inputs into researched ones.

Remedy Sheet #1 (hashtags & search terms), layer by layer:

  `translit`     — Uzbek script fan-out (Latin ⇄ Cyrillic, apostrophe variants).
  `patterns`     — the demand-side request-pattern matrix: buyers write
                   "videograf kerak", not "#videography".
  `autocomplete` — free, keyless query mining from Google/YouTube suggest, which
                   reports what real users in a locale actually type.
  `expand`       — the orchestrator: LLM-proposed nouns in, ranked real queries out.
  `banned`       — a zero-request prefilter: known-banned, too-short and
                   too-generic tags never reach a validator or a browser.
  `validate`     — per-platform "does this term actually work" probes, where the
                   research says probing is safe (YouTube API, IG typeahead).
  `prescore`     — the same question for seed ACCOUNTS (Sheet #2): one request per
                   candidate, plus the liveness gate that reads the answer.
  `buyer_density`— the moat metric: price-intent questions per 100 comments,
                   demand separated from supply by DIRECTION, not by keyword.

Everything here is OPTIONAL and offline-tolerant by construction: a network
failure degrades to fewer candidates, never to a failed campaign draft.
"""
from .banned import prefilter, reason_to_skip             # noqa: F401
from .expand import ExpansionResult, expand_seeds         # noqa: F401
from .patterns import demand_queries, request_patterns    # noqa: F401
from .translit import script_variants                     # noqa: F401
from .buyer_density import (BuyerDensity, classify_comment,  # noqa: F401
                            rank_candidates, score_comments)
from .prescore import (AccountProfile, GateVerdict,        # noqa: F401
                       liveness_gate, probe_for)
from .validate import (DEAD, LIVE, THIN, UNKNOWN,         # noqa: F401
                       TermVerdict, partition, validators_for)
