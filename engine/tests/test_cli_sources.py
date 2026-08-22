"""`aizu sources` — the operator-facing view of the per-source discovery ledger
(Campaign Lab, Remedy Sheet #1 / Remedy D)."""
import os
import tempfile

import pytest

from aizu.cli import build_parser
from aizu.core.store import Store


def main(argv):
    """Parse + dispatch WITHOUT `cli.main`'s `_load_env()`, which imports
    `engine/.env` into `os.environ` for the rest of the session."""
    args = build_parser().parse_args(argv)
    return args.func(args)


@pytest.fixture
def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path


def _seed(path):
    store = Store(path)
    store.record_source_walk("c1", "remont", kind="hashtag", yielded=12)
    store.record_source_walk("c1", "acme", kind="account", carried_over=12)
    store.record_source_walk("c1", "gone", kind="hashtag", unavailable=True)
    for i in range(4):
        store.mark_seen("c1", f"r{i}", relevant=True, source="remont",
                        caption="#remont #dizayn")
    store.upsert_match(campaign_id="c1", reel_id="r0", comment_id="k1",
                       username="u", text="narxi?", lang="uz", score=0.9,
                       reason="asks price", extracted=None, tier="text")
    store.close()


def test_reports_yield_carry_over_and_ban(db, capsys):
    _seed(db)
    assert main(["--db", db, "sources", "--campaign", "c1"]) == 0
    out = capsys.readouterr().out
    assert "remont" in out and "BANNED" in out
    # The account carried 12 reels it never produced — the 2026-08-19 confusion.
    acme = next(l for l in out.splitlines() if l.startswith("acme"))
    assert acme.split()[2:5] == ["1", "0", "12"]   # navs, yield, carried


def test_mine_ranks_co_occurring_tags_from_our_own_captions(db, capsys):
    _seed(db)
    assert main(["--db", db, "sources", "--campaign", "c1", "--mine"]) == 0
    out = capsys.readouterr().out
    assert "dizayn" in out


def test_a_campaign_with_no_ledger_says_so_rather_than_printing_nothing(db, capsys):
    Store(db).close()
    assert main(["--db", db, "sources", "--campaign", "nope"]) == 0
    assert "No source data yet" in capsys.readouterr().out


# ----- `aizu seeds` (Campaign Lab, Remedy Sheet #2/A) -----

def _seed_authors(path):
    store = Store(path)
    store.record_source_walk("c1", "tech", kind="hashtag", yielded=5)
    for rid, author, aid in (("r1", "MKBHD", "UC_mk"), ("r2", "MKBHD", "UC_mk"),
                             ("r3", "Quiet", "UC_q")):
        store.mark_seen("c1", rid, relevant=True, author=author, author_id=aid,
                        source="tech")
    store.upsert_match(campaign_id="c1", reel_id="r1", comment_id="k1",
                       username="buyer", text="narxi?", lang="uz", score=0.9,
                       reason="asks price", extracted=None, tier="text")
    store.close()


def test_seeds_ranks_accounts_by_proof(db, capsys):
    _seed_authors(db)
    assert main(["--db", db, "seeds", "--campaign", "c1"]) == 0
    out = capsys.readouterr().out
    # UC_mk produced a lead, UC_q did not.
    assert out.index("UC_mk") < out.index("UC_q")
    assert "MKBHD" in out


def test_seeds_hides_accounts_below_the_bar(db, capsys):
    _seed_authors(db)
    assert main(["--db", db, "seeds", "--campaign", "c1", "--min-relevant", "2"]) == 0
    out = capsys.readouterr().out
    assert "UC_mk" in out and "UC_q" not in out


def test_seeds_says_so_when_there_is_nothing_to_propose(db, capsys):
    Store(db).close()
    assert main(["--db", db, "seeds", "--campaign", "nope"]) == 0
    assert "No seed candidates" in capsys.readouterr().out


# ----- `aizu gold` — the labelling queue (Sheet #3 / Remedy E) -----

def _candidates(path):
    store = Store(path)
    for cid, text, lang, score, band in (
            ("k1", "narxi qancha?", "uz", 0.68, "near"),
            ("k2", "ajoyib 🔥", "uz", 0.05, "clear"),
            ("k3", "Цена?", "ru", 0.82, "accepted")):
        store.record_eval_candidate(campaign_id="c1", comment_id=cid, text=text,
                                    band=band, score=score, threshold=0.7,
                                    lang=lang, username="u", session_id="s1")
    store.close()


def test_gold_lists_the_queue_most_informative_first(db, capsys):
    _candidates(db)
    assert main(["--db", db, "gold", "--campaign", "c1"]) == 0
    out = capsys.readouterr().out
    assert out.index("k1") < out.index("k3") < out.index("k2")
    assert "3 captured · 0 labelled" in out


def test_gold_records_a_human_verdict(db, capsys):
    _candidates(db)
    assert main(["--db", db, "gold", "--campaign", "c1",
                 "--label", "k1", "--verdict", "yes", "--by", "ali"]) == 0
    store = Store(db)
    row = next(r for r in store.eval_candidates("c1", limit=99)
               if r["comment_id"] == "k1")
    assert row["label"] == 1 and row["labeled_by"] == "ali"
    store.close()


def test_labelled_items_leave_the_unlabelled_queue(db, capsys):
    _candidates(db)
    main(["--db", db, "gold", "--campaign", "c1", "--label", "k1", "--verdict", "no"])
    capsys.readouterr()
    main(["--db", db, "gold", "--campaign", "c1"])
    assert "k1" not in capsys.readouterr().out


def test_gold_exports_the_shape_run_eval_already_reads(db, tmp_path, capsys):
    import json
    _candidates(db)
    main(["--db", db, "gold", "--campaign", "c1", "--label", "k1", "--verdict", "yes"])
    main(["--db", db, "gold", "--campaign", "c1", "--label", "k2", "--verdict", "no"])
    out = tmp_path / "gold.json"
    assert main(["--db", db, "gold", "--campaign", "c1",
                 "--all", "--export", str(out)]) == 0
    items = json.loads(out.read_text())
    assert {i["text"]: i["match"] for i in items} == {"narxi qancha?": True,
                                                     "ajoyib 🔥": False}
    assert all(set(i) >= {"text", "lang", "match"} for i in items)  # gold.json shape


def test_gold_says_so_when_nothing_has_been_captured(db, capsys):
    Store(db).close()
    assert main(["--db", db, "gold", "--campaign", "nope"]) == 0
    assert "No candidates" in capsys.readouterr().out
