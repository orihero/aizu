"""Statistics discipline for the prompt/threshold eval harness.

Campaign Lab, Remedy Sheet #3 / Remedy D.3. `scripts/eval/run_eval.py` reports
bare precision/recall/F1 point estimates, and on a 25-item gold set a point
estimate is close to meaningless: at n=25 the 95% interval around a precision of
0.80 spans roughly 0.59-0.93. Two variants "differing by 4 points" are
indistinguishable noise, and a regression suite built on that comparison will
chase ghosts and miss real drops.

Everything here is pure, deterministic (bootstrap takes an explicit seed) and
free of I/O, so it can be unit-tested without a model, a key or a network.

The four disciplines the sheet asks for:
  * `wilson_interval` — never a point estimate.
  * `mcnemar_exact` + `paired_flips` — a NET-ZERO delta with 12 flips is a moved
    boundary, not a no-op; only the paired discordant counts can tell you.
  * `sweep_threshold` — max precision subject to a recall floor, placed mid-gap
    between score clusters rather than on top of one.
  * `baseline_key` — a result is only comparable to another result produced by the
    same prompt, model, params AND threshold.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional, Sequence

# 95% two-sided normal quantile.
Z95 = 1.959963984540054


# --------------------------------------------------------------------------- #
# Interval estimation
# --------------------------------------------------------------------------- #

def wilson_interval(successes: int, trials: int, z: float = Z95) -> tuple[float, float]:
    """95% Wilson score interval for a binomial proportion.

    Wilson rather than normal-approximation ("Wald") because Wald is badly wrong
    exactly where eval sets live: small n, and proportions near 0 or 1, where it
    happily produces bounds below 0 or above 1 and its coverage collapses.

    Zero trials → (0.0, 1.0): no evidence is total uncertainty, not certainty."""
    if trials <= 0:
        return (0.0, 1.0)
    phat = successes / trials
    denom = 1.0 + (z * z) / trials
    centre = (phat + (z * z) / (2 * trials)) / denom
    margin = (z * math.sqrt((phat * (1 - phat) + (z * z) / (4 * trials)) / trials)
              / denom)
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def wilson_lower_bound(successes: int, trials: int, z: float = Z95) -> float:
    """Just the lower bound — the ranking-safe estimate (see `core/tagmine.py`)."""
    if trials <= 0:
        return 0.0
    return wilson_interval(successes, trials, z)[0]


@dataclass(frozen=True)
class Metric:
    """A proportion with its interval. `n` is the DENOMINATOR, which is what makes
    the interval interpretable — precision and recall have different ones."""
    name: str
    hits: int
    n: int

    @property
    def value(self) -> float:
        return (self.hits / self.n) if self.n else 0.0

    @property
    def interval(self) -> tuple[float, float]:
        return wilson_interval(self.hits, self.n)

    @property
    def width(self) -> float:
        lo, hi = self.interval
        return hi - lo

    def __str__(self) -> str:
        lo, hi = self.interval
        return f"{self.name}={self.value:.3f} [{lo:.3f}-{hi:.3f}] n={self.n}"

    def as_dict(self) -> dict[str, Any]:
        lo, hi = self.interval
        return {"name": self.name, "value": round(self.value, 4), "n": self.n,
                "hits": self.hits, "lo": round(lo, 4), "hi": round(hi, 4)}


@dataclass
class Confusion:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0

    @property
    def n(self) -> int:
        return self.tp + self.fp + self.fn + self.tn

    @property
    def precision(self) -> Metric:
        return Metric("precision", self.tp, self.tp + self.fp)

    @property
    def recall(self) -> Metric:
        return Metric("recall", self.tp, self.tp + self.fn)

    @property
    def accuracy(self) -> Metric:
        return Metric("accuracy", self.tp + self.tn, self.n)

    @property
    def f1(self) -> float:
        """Reported WITHOUT an interval on purpose: F1 is a ratio of ratios with
        no clean binomial interpretation, so a Wilson interval on it would be a
        made-up number. Compare precision and recall separately."""
        p, r = self.precision.value, self.recall.value
        return (2 * p * r / (p + r)) if (p + r) else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"tp": self.tp, "fp": self.fp, "fn": self.fn, "tn": self.tn,
                "precision": self.precision.as_dict(),
                "recall": self.recall.as_dict(),
                "f1": round(self.f1, 4)}


def confusion(predictions: Iterable[bool], truths: Iterable[bool]) -> Confusion:
    c = Confusion()
    for pred, truth in zip(predictions, truths):
        if pred and truth:
            c.tp += 1
        elif pred and not truth:
            c.fp += 1
        elif not pred and truth:
            c.fn += 1
        else:
            c.tn += 1
    return c


# --------------------------------------------------------------------------- #
# Paired comparison — the only honest way to compare two prompt variants
# --------------------------------------------------------------------------- #

@dataclass
class FlipReport:
    """What changed between a baseline run and a candidate run, ITEM BY ITEM.

    The headline number an unpaired comparison gives you is the net delta, and a
    net delta of zero routinely hides a moved decision boundary: 6 items fixed and
    6 items broken reads as "no change". `gained`/`lost` are the discordant pairs
    McNemar is computed from, and they are also the list a human actually needs to
    read."""
    gained: list[Any] = field(default_factory=list)   # wrong → right
    lost: list[Any] = field(default_factory=list)     # right → wrong
    unchanged: int = 0

    @property
    def net(self) -> int:
        return len(self.gained) - len(self.lost)

    @property
    def discordant(self) -> int:
        return len(self.gained) + len(self.lost)

    @property
    def p_value(self) -> float:
        return mcnemar_exact(len(self.gained), len(self.lost))

    def significant(self, alpha: float = 0.05) -> bool:
        return self.p_value < alpha

    def summary(self) -> str:
        return (f"net={self.net:+d} (+{len(self.gained)}/-{len(self.lost)}) "
                f"discordant={self.discordant} p={self.p_value:.4f}"
                f"{' SIGNIFICANT' if self.significant() else ''}")

    def as_dict(self) -> dict[str, Any]:
        return {"gained": list(self.gained), "lost": list(self.lost),
                "unchanged": self.unchanged, "net": self.net,
                "discordant": self.discordant, "pValue": round(self.p_value, 6),
                "significant": self.significant()}


def paired_flips(baseline: Sequence[bool], candidate: Sequence[bool],
                 truths: Sequence[bool],
                 ids: Optional[Sequence[Any]] = None) -> FlipReport:
    """Per-item flip list between two runs over the SAME frozen set.

    Requires equal-length aligned sequences — comparing runs over different item
    sets is the mistake this signature exists to make impossible."""
    if not (len(baseline) == len(candidate) == len(truths)):
        raise ValueError("baseline, candidate and truths must be the same length")
    labels = list(ids) if ids is not None else list(range(len(truths)))
    if len(labels) != len(truths):
        raise ValueError("ids must be the same length as truths")
    rep = FlipReport()
    for label, b, c, t in zip(labels, baseline, candidate, truths):
        was_right, is_right = (b == t), (c == t)
        if was_right == is_right:
            rep.unchanged += 1
        elif is_right:
            rep.gained.append(label)
        else:
            rep.lost.append(label)
    return rep


def mcnemar_exact(gained: int, lost: int) -> float:
    """Two-sided exact McNemar p-value from the discordant counts.

    EXACT (binomial), not the chi-squared approximation: eval flip counts are
    routinely under 25, which is exactly where the approximation is unreliable
    and where a continuity-corrected chi-squared is over-conservative.

    No discordant pairs → p = 1.0: the two runs agree everywhere, which is the
    strongest possible evidence of no difference, not a missing answer."""
    n = gained + lost
    if n == 0:
        return 1.0
    k = min(gained, lost)
    tail = sum(math.comb(n, i) for i in range(k + 1)) * (0.5 ** n)
    return min(1.0, 2.0 * tail)


def self_flip_rate(run_a: Sequence[bool], run_b: Sequence[bool]) -> float:
    """Disagreement between two runs of the SAME configuration — the noise floor.

    Any measured improvement smaller than this is indistinguishable from the
    model's own run-to-run variance, and reporting it as a win is how a prompt
    "improvement" survives to production without ever having helped."""
    if not run_a or len(run_a) != len(run_b):
        return 0.0
    return sum(1 for a, b in zip(run_a, run_b) if a != b) / len(run_a)


# --------------------------------------------------------------------------- #
# Threshold selection
# --------------------------------------------------------------------------- #

@dataclass
class ThresholdChoice:
    threshold: float
    precision: Metric
    recall: Metric
    reason: str = ""
    stability: Optional[tuple[float, float]] = None   # bootstrap 5th/95th pct

    def as_dict(self) -> dict[str, Any]:
        return {"threshold": round(self.threshold, 4),
                "precision": self.precision.as_dict(),
                "recall": self.recall.as_dict(), "reason": self.reason,
                "stability": (None if self.stability is None
                              else [round(x, 4) for x in self.stability])}


def sweep_threshold(scores: Sequence[float], truths: Sequence[bool], *,
                    min_recall: float = 0.9,
                    bootstrap: int = 0, seed: int = 0) -> ThresholdChoice:
    """Pick the threshold with max precision subject to `recall >= min_recall`.

    Two details that matter more than the search itself:

    * The chosen value is placed MID-GAP between the two score clusters it
      separates, not on top of an observed score. Model-verbalized scores collapse
      onto a handful of values (0.7/0.8/0.9), so a threshold sitting exactly on
      0.70 decides a large block of items by floating-point luck.
    * `bootstrap` resamples the set to report how stable the choice is. A
      threshold whose 5th-95th percentile spans 0.4-0.9 has not been measured, it
      has been guessed — which is the failure the sheet names as "LLM-guessed
      thresholds" wearing a number.

    Falls back to the highest-recall threshold when the recall floor is
    unreachable, and says so in `reason` rather than silently returning something
    that misses the requirement.
    """
    if not scores or len(scores) != len(truths):
        raise ValueError("scores and truths must be non-empty and the same length")
    best = _best_threshold(scores, truths, min_recall)
    choice = ThresholdChoice(threshold=best[0], precision=best[1], recall=best[2],
                             reason=best[3])
    if bootstrap > 0:
        rng = random.Random(seed)
        n = len(scores)
        picks = []
        for _ in range(bootstrap):
            idx = [rng.randrange(n) for _ in range(n)]
            s = [scores[i] for i in idx]
            t = [truths[i] for i in idx]
            try:
                picks.append(_best_threshold(s, t, min_recall)[0])
            except ValueError:  # pragma: no cover - resample degenerate
                continue
        if picks:
            picks.sort()
            lo = picks[max(0, int(0.05 * len(picks)) - 1)]
            hi = picks[min(len(picks) - 1, int(0.95 * len(picks)))]
            choice.stability = (lo, hi)
    return choice


def _best_threshold(scores: Sequence[float], truths: Sequence[bool],
                    min_recall: float) -> tuple[float, Metric, Metric, str]:
    # Candidate cut points sit BETWEEN adjacent distinct scores (plus the two
    # outer edges), which is what makes the result mid-gap by construction.
    uniq = sorted(set(scores))
    cuts = [uniq[0] - 1e-6]
    cuts += [(a + b) / 2.0 for a, b in zip(uniq, uniq[1:])]
    cuts.append(uniq[-1] + 1e-6)
    scored: list[tuple[float, Confusion]] = []
    for cut in cuts:
        scored.append((cut, confusion([s >= cut for s in scores], truths)))
    eligible = [(cut, c) for cut, c in scored if c.recall.value >= min_recall]
    if eligible:
        # Max precision; ties break toward the HIGHER threshold, which is the
        # more conservative gate for the same measured precision.
        cut, conf = max(eligible, key=lambda kc: (kc[1].precision.value, kc[0]))
        return (cut, conf.precision, conf.recall,
                f"max precision at recall >= {min_recall:.2f}")
    cut, conf = max(scored, key=lambda kc: (kc[1].recall.value, kc[1].precision.value))
    return (cut, conf.precision, conf.recall,
            f"recall floor {min_recall:.2f} unreachable; best available recall")


# --------------------------------------------------------------------------- #
# Comparability
# --------------------------------------------------------------------------- #

def baseline_key(*, prompt: str, model: str, threshold: float,
                 params: Optional[dict[str, Any]] = None,
                 gold_ids: Optional[Sequence[Any]] = None) -> str:
    """A stable id for "the configuration this result describes".

    Two results are only comparable when this matches. Including the GOLD SET's
    item ids is deliberate and goes beyond the sheet: adding items to the set
    changes every measurement, and a stored baseline that silently spans two
    different sets is worse than no baseline."""
    payload = {
        "prompt": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "model": model,
        "threshold": round(float(threshold), 6),
        "params": params or {},
        "gold": (hashlib.sha256(
            json.dumps(sorted(map(str, gold_ids)), ensure_ascii=False)
            .encode("utf-8")).hexdigest() if gold_ids is not None else None),
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def slice_report(items: Iterable[Any], *, predicted: Callable[[Any], bool],
                 truth: Callable[[Any], bool],
                 slicer: Callable[[Any], str],
                 min_n: int = 30) -> dict[str, dict[str, Any]]:
    """Per-slice confusion + intervals, flagging slices too small to conclude from.

    The sheet asks for >=30 items per language/script slice; anything under that
    is reported with `underpowered: true` so a per-slice number is never quoted as
    if it were measured."""
    buckets: dict[str, list[Any]] = {}
    for item in items:
        buckets.setdefault(str(slicer(item)), []).append(item)
    out: dict[str, dict[str, Any]] = {}
    for name, group in sorted(buckets.items()):
        conf = confusion([predicted(i) for i in group], [truth(i) for i in group])
        row = conf.as_dict()
        row["n"] = len(group)
        row["underpowered"] = len(group) < min_n
        out[name] = row
    return out
