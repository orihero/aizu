"""Generate leads over ~N reels of live discovery.

Walks campaign seed hashtags/accounts + home feed, opens each relevant reel
full-screen to read its comments (and like/follow per the campaign's opt-in
policy), and accumulates leads. Read-only except the capped opt-in engagement.
Daytime guard relaxed for this operator-requested run; stops on any halt
(action-block / canary / login) or when the reel target is reached.
"""
import json
import sqlite3
import time
import urllib.request
from pathlib import Path

from aizu.cli import _load_env
_load_env()
from aizu.core.config import load_campaign, load_soul
from aizu.engines.instagram.cdp import CDPConfig, CDPFeed
from aizu.core.pacing import Pacer, PacingConfig
from aizu.core.router import OpenRouterRouter
from aizu.engines.instagram.session import HaltSession, Session, SessionConfig
from aizu.core.store import Store

DB = "aizu.db"
CDP_URL = "http://127.0.0.1:9333"
TARGET_REELS = 100
REELS_PER_SESSION = 25
PER_SOURCE_REELS = 10
SPEND_CAP = 5.0

soul = load_soul(Path("config/soul.md"))
campaign = load_campaign(Path("config/campaign.md"))


def ensure_tab():
    try:
        with urllib.request.urlopen(f"{CDP_URL}/json/list", timeout=5) as r:
            targets = json.load(r)
        if not any(t.get("type") == "page" for t in targets):
            req = urllib.request.Request(
                f"{CDP_URL}/json/new?https://www.instagram.com/reels/", method="PUT")
            urllib.request.urlopen(req, timeout=5).read()
            time.sleep(2)
    except Exception as e:  # noqa: BLE001
        print(f">> ensure_tab warning: {e}", flush=True)


def lead_count():
    db = sqlite3.connect(DB)
    n = db.execute("select count(*) from matches").fetchone()[0]
    db.close()
    return n


def run():
    baseline = lead_count()
    print(f">> LEAD SWEEP · target {TARGET_REELS} reels · seeds={list(campaign.seed_hashtags)}"
          f"+{list(campaign.seed_accounts)} · actions={'on' if campaign.enable_actions else 'off'}",
          flush=True)
    print(f">> existing leads in DB: {baseline}", flush=True)

    total_reels = 0
    session_no = 0
    while total_reels < TARGET_REELS:
        session_no += 1
        ensure_tab()
        store = Store(DB)
        router = OpenRouterRouter(store=store, spend_cap_usd=SPEND_CAP)
        feed = CDPFeed(CDPConfig(
            cdp_url=CDP_URL,
            seed_hashtags=tuple(campaign.seed_hashtags),
            seed_accounts=tuple(campaign.seed_accounts),
            per_source_reels=PER_SOURCE_REELS))
        remaining = TARGET_REELS - total_reels
        budget = min(REELS_PER_SESSION, remaining)
        pacer = Pacer(cfg=PacingConfig(
            enforce_daytime=False, dwell_min=2, dwell_max=5,
            between_min=1, between_max=3,
            reels_per_session_min=budget, reels_per_session_max=budget))
        try:
            feed.attach()
            summary = Session(store=store, router=router, feed=feed, soul=soul,
                              campaign=campaign, pacer=pacer, cfg=SessionConfig()).run()
        except HaltSession as h:
            print(f">> [session {session_no}] HALTED: {h.reason} — stopping sweep.", flush=True)
            store.close()
            break
        except Exception as e:  # noqa: BLE001
            print(f">> [session {session_no}] ERROR: {type(e).__name__}: {str(e)[:120]}", flush=True)
            try:
                feed.close()
            finally:
                store.close()
            time.sleep(8)
            continue
        finally:
            try:
                feed.close()
            except Exception:
                pass

        total_reels += summary["reels_seen"]
        print(f">> [session {session_no}] reels={summary['reels_seen']} "
              f"relevant={summary['relevance_passes']} NEW-matches={summary['matches']} "
              f"likes={summary['likes']} follows={summary['follows']} "
              f"esc={summary['escalations']} spend=${summary['spend_usd']} "
              f"| cumulative reels={total_reels}/{TARGET_REELS}", flush=True)
        store.close()

        if summary["feed_health_flag"]:
            print(">> tired-feed flag — feed tapping out; continuing across remaining seeds.",
                  flush=True)
        time.sleep(12)  # human pause between sessions

    report(baseline)


def report(baseline):
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    new_leads = lead_count() - baseline
    print(f"\n>> SWEEP DONE · {new_leads} new lead(s) this sweep", flush=True)
    rows = db.execute(
        "select username, lang, score, text, extracted, reel_id from matches "
        "order by rowid desc limit ?", (max(0, new_leads),)).fetchall()
    for r in rows:
        print(f"   {r['score']:.2f} [{r['lang']}] @{r['username']}: {r['text'][:90]}", flush=True)
        print(f"        extracted: {r['extracted']} (reel {r['reel_id']})", flush=True)
    ac = db.execute("select action_type, count(*) n from actions where succeeded=1 group by action_type").fetchall()
    print(">> engagement this account:", {r["action_type"]: r["n"] for r in ac} or "none", flush=True)
    db.close()


if __name__ == "__main__":
    run()
