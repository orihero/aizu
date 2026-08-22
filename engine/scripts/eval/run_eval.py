"""Prompt eval harness for the engine's two LLM decisions.

Two tasks:
  match     — is this COMMENT a lead? gold.json, variants/, decide at threshold (0.70).
  relevance — is this REEL caption on-campaign? gold_relevance.json,
              variants_relevance/, decide at the gate cutoff (score >= 0.5).

A variant lives in <variants_dir>/<name>.py and exposes SYSTEM (str) and
USER_TEMPLATE (str with {brief} and {content}). Runs the REAL configured model
and reports precision / recall / F1 + a per-item table so regressions show.

Every proportion carries a 95% Wilson interval (`aizu.core.evalstats`). On a
25-item gold set a bare point estimate is close to meaningless — the interval
around a precision of 0.80 spans roughly 0.59-0.93 — so two variants "differing
by 4 points" are indistinguishable noise. Comparing two variants also prints the
PAIRED flip list and an exact McNemar p-value, because a net-zero delta routinely
hides a moved decision boundary (Campaign Lab, Remedy Sheet #3 / Remedy D.3).

Usage:
  ./.venv/bin/python scripts/eval/run_eval.py --task match --all
  ./.venv/bin/python scripts/eval/run_eval.py --task relevance v2_relevance current_relevance
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from aizu.cli import _load_env  # noqa: E402
_load_env()
from aizu.engines.instagram.cascade import _comment_content  # noqa: E402
from aizu.core.config import load_campaign  # noqa: E402
from aizu.core.feed import Comment, Reel  # noqa: E402
from aizu.core.evalstats import (baseline_key, confusion, paired_flips,  # noqa: E402
                                 slice_report, sweep_threshold)
from aizu.core.router import (Decision, OpenRouterRouter, _content_or_none,  # noqa: E402
                              _decision_from_payload, _extract_json)

EVAL_DIR = Path(__file__).resolve().parent
CAMPAIGN = load_campaign(ROOT / "config" / "campaign.md")

TASKS = {
    "match": {
        "gold": "gold.json",
        "variants": "variants",
        "brief": f"{CAMPAIGN.match_def}\n\nEXTRACT FIELDS:\n{CAMPAIGN.extract_def}",
        "cutoff": CAMPAIGN.threshold,        # match at >= threshold
    },
    "relevance": {
        "gold": "gold_relevance.json",
        "variants": "variants_relevance",
        "brief": CAMPAIGN.relevance_def,
        "cutoff": 0.5,                        # gate keeps a reel at score >= 0.5
    },
}


def load_variant(variants_dir: str, name: str):
    path = EVAL_DIR / variants_dir / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"variant_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.SYSTEM, mod.USER_TEMPLATE


def score_one(router: OpenRouterRouter, system: str, user: str) -> Decision | None:
    payload = {
        "model": router.text_model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0,
        "usage": {"include": True},
    }
    try:
        body = router._post(payload)
    except Exception:
        return None
    text = _content_or_none(body)
    if text is None:
        return None
    return _decision_from_payload(_extract_json(text), "cloud", 0.0, text)


def item_content(item: dict) -> str:
    """Mirror exactly what the cascade sends in production: the bare comment,
    or comment + reel-context block when the gold item carries a reel."""
    spec = item.get("reel")
    reel = Reel("eval", author=spec.get("author", ""), caption=spec.get("caption", ""),
                ocr_text=spec.get("ocr", "")) if spec else None
    return _comment_content(Comment("eval", "eval", item["text"]), reel)


# null expected → these actual values count as "correctly not extracted"
_NULLISH = {"", "none", "null", "unknown"}


def check_extraction(expected: dict, extracted: dict) -> tuple[int, int, list[str]]:
    """Per-field check: null → must be absent/null-ish; string/list → any of
    the accepted substrings must appear (case-insensitive)."""
    ok, failures = 0, []
    for field, want in expected.items():
        got = extracted.get(field)
        got_str = str(got).strip() if got is not None else ""
        if want is None:
            if got is None or got_str.lower() in _NULLISH:
                ok += 1
            else:
                failures.append(f"{field}: want null, got {got!r}")
        else:
            accepted = [want] if isinstance(want, str) else want
            if got_str and any(a.lower() in got_str.lower() for a in accepted):
                ok += 1
            else:
                failures.append(f"{field}: want one of {accepted}, got {got!r}")
    return ok, len(expected), failures


def evaluate(task: str, name: str) -> dict:
    cfg = TASKS[task]
    gold = json.loads((EVAL_DIR / cfg["gold"]).read_text(encoding="utf-8"))
    system, template = load_variant(cfg["variants"], name)
    router = OpenRouterRouter()
    tp = fp = tn = fn = errors = 0
    extract_ok = extract_total = 0
    rows = []
    # Kept per item so the paired comparison below comes from the SAME frozen set
    # in the SAME order — an unpaired comparison of two runs cannot tell a fixed
    # item from a broken one.
    preds: list[bool] = []
    truths: list[bool] = []
    scores: list[float] = []
    ids: list[str] = []
    langs: list[str] = []
    for item in gold:
        user = template.format(brief=cfg["brief"], content=item_content(item))
        decision = score_one(router, system, user)
        if decision is None:
            errors += 1
            pred, score = False, -1.0
        else:
            score = decision.score
            pred = score >= cfg["cutoff"]
        g = item["match"]
        preds.append(pred)
        truths.append(bool(g))
        scores.append(score)
        ids.append(str(item.get("id") or item["text"][:60]))
        langs.append(str(item.get("lang") or "??"))
        if pred and g: tp += 1
        elif pred and not g: fp += 1
        elif not pred and g: fn += 1
        else: tn += 1
        mark = "ok " if pred == g else "XX "
        rows.append(f"  {mark} gold={'Y' if g else 'n'} pred={'Y' if pred else 'n'} "
                    f"score={score:5.2f}  {item['text'][:46]}")
        expected = item.get("expect_extracted")
        if expected and decision is not None:
            n_ok, n_total, failures = check_extraction(
                expected, decision.extracted if isinstance(decision.extracted, dict) else {})
            extract_ok += n_ok
            extract_total += n_total
            for f in failures:
                rows.append(f"      extract XX  {f}")
    conf = confusion(preds, truths)
    return {"name": name, "tp": tp, "fp": fp, "tn": tn, "fn": fn, "errors": errors,
            "precision": conf.precision, "recall": conf.recall, "f1": conf.f1,
            "rows": rows, "preds": preds, "truths": truths, "scores": scores,
            "ids": ids, "langs": langs,
            "key": baseline_key(prompt=system, model=router.text_model,
                                threshold=cfg["cutoff"], gold_ids=ids),
            "extract_ok": extract_ok, "extract_total": extract_total}


def main(argv: list[str]) -> int:
    task = "match"
    if "--task" in argv:
        i = argv.index("--task")
        task = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    cfg = TASKS[task]
    names = argv
    if not names or names == ["--all"]:
        names = sorted(p.stem for p in (EVAL_DIR / cfg["variants"]).glob("*.py")
                       if p.stem != "__init__")
    gold = json.loads((EVAL_DIR / cfg["gold"]).read_text(encoding="utf-8"))
    print(f"task={task} cutoff={cfg['cutoff']} · {len(gold)} gold items · "
          f"model={OpenRouterRouter().text_model}\n")
    results = []
    for name in names:
        r = evaluate(task, name)
        results.append(r)
        print(f"=== variant: {name} ===")
        print("\n".join(r["rows"]))
        extract = (f" extraction={r['extract_ok']}/{r['extract_total']}"
                   if r["extract_total"] else "")
        print(f"  -> {r['precision']}  {r['recall']}  F1={r['f1']:.2f}{extract}")
        print(f"     (tp={r['tp']} fp={r['fp']} fn={r['fn']} tn={r['tn']} "
              f"errors={r['errors']})  baseline={r['key']}")
        # Per-language slices, with the ones too small to conclude from marked.
        slices = slice_report(
            range(len(r["truths"])), predicted=lambda i: r["preds"][i],
            truth=lambda i: r["truths"][i], slicer=lambda i: r["langs"][i])
        for lang, row in slices.items():
            flag = "  UNDERPOWERED (<30)" if row["underpowered"] else ""
            print(f"     [{lang}] n={row['n']} p={row['precision']['value']:.2f} "
                  f"r={row['recall']['value']:.2f}{flag}")
        # What threshold would this variant's own scores have wanted?
        if any(s >= 0 for s in r["scores"]):
            choice = sweep_threshold(r["scores"], r["truths"], min_recall=0.9,
                                     bootstrap=200, seed=0)
            stab = (f" stability={choice.stability[0]:.2f}-{choice.stability[1]:.2f}"
                    if choice.stability else "")
            print(f"     best threshold={choice.threshold:.3f} ({choice.reason})"
                  f"{stab}")
        print()
    if len(results) > 1:
        best = max(results, key=lambda x: (x["f1"], -x["fp"]))
        print("=== summary (by F1, then fewest false-positives) ===")
        for r in sorted(results, key=lambda x: (-x["f1"], x["fp"])):
            star = " <== WINNER" if r["name"] == best["name"] else ""
            print(f"  {r['name']:18} F1={r['f1']:.2f} "
                  f"P={r['precision'].value:.2f} R={r['recall'].value:.2f} "
                  f"fp={r['fp']}{star}")
        # PAIRED comparison against the first variant named. This is the number
        # that decides whether a difference is real: a net-zero delta with twelve
        # flips is a moved boundary, not a no-op.
        base = results[0]
        print(f"\n=== paired vs {base['name']} (McNemar, exact) ===")
        for r in results[1:]:
            flips = paired_flips(base["preds"], r["preds"], base["truths"],
                                 ids=base["ids"])
            print(f"  {r['name']:18} {flips.summary()}")
            for label in flips.lost:
                print(f"      BROKE  {label[:70]}")
            for label in flips.gained:
                print(f"      fixed  {label[:70]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
