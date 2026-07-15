from pathlib import Path

import pytest

from reelradar.core.config import (ChannelSpec, _channel_spec_from_dict,
                              _resolve_home_feed, campaign_from_brief,
                              campaign_to_brief, load_campaign, load_soul,
                              parse_extract_fields)
from reelradar.core.prompts import SYSTEM_GENERIC, VISION_GENERIC
from reelradar.engines.instagram.prompts import (SYSTEM_MATCH, SYSTEM_RELEVANCE,
                                                 SYSTEM_VISION)

CONFIG = Path(__file__).resolve().parents[1] / "config"


def test_campaign_parses():
    c = load_campaign(CONFIG / "campaign.md")
    assert c.campaign_id == "acme-saas-leadgen"
    assert c.goal == "lead"
    assert c.threshold == 0.70
    assert c.escalate_band == (0.40, 0.75)
    assert "en" in c.language_mix
    assert c.relevance_def and c.match_def and c.extract_def


def test_campaign_parses_seeds_and_action_knobs():
    c = load_campaign(CONFIG / "campaign.md")
    assert "saas" in c.seed_hashtags
    assert "acme.io" in c.seed_accounts
    # Harvest is read-only: engagement now belongs to the warming engine, so the
    # shipped campaign ships enable_actions: false (warming PRD C1 / §4.3).
    assert c.enable_actions is False
    assert c.max_likes_per_session == 8
    assert c.max_follows_per_session == 4


def test_action_knobs_default_off(tmp_path):
    # A campaign that omits the knobs must default to read-only / safe caps.
    md = tmp_path / "c.md"
    md.write_text(
        "```yaml\ncampaign_id: x\ngoal: lead\nthreshold: 0.7\nescalate_band: [0.4, 0.75]\n```\n"
        "## Relevance\nr\n## Match\nm\n## Extract\ne\n", encoding="utf-8")
    c = load_campaign(md)
    assert c.enable_actions is False
    assert c.seed_hashtags == [] and c.seed_accounts == []
    assert c.max_likes_per_session == 8 and c.max_follows_per_session == 4


def test_campaign_prompts_match_reference_verbatim():
    """The shipped default campaign must carry the reference prompts byte-for-byte
    so moving them out of code into campaign.md is a no-op for live decisions
    (the regression invariant of the generic-prompt refactor)."""
    c = load_campaign(CONFIG / "campaign.md")
    assert c.relevance_prompt == SYSTEM_RELEVANCE
    assert c.match_prompt == SYSTEM_MATCH
    assert c.vision_prompt == SYSTEM_VISION


def test_campaign_without_prompt_sections_falls_back_empty(tmp_path):
    """A thin campaign that omits the prompt sections leaves the fields empty;
    the router then substitutes its domain-free generic prompt."""
    md = tmp_path / "c.md"
    md.write_text(
        "```yaml\ncampaign_id: x\ngoal: lead\nthreshold: 0.7\nescalate_band: [0.4, 0.75]\n```\n"
        "## Relevance\nr\n## Match\nm\n## Extract\ne\n", encoding="utf-8")
    c = load_campaign(md)
    assert c.relevance_prompt == "" and c.match_prompt == "" and c.vision_prompt == ""
    # the generic fallbacks exist for the router to use
    assert SYSTEM_GENERIC and VISION_GENERIC


def test_platform_defaults_to_instagram():
    c = load_campaign(CONFIG / "campaign.md")
    assert c.platform == "instagram"
    assert c.seed_channels == []


def _thin_campaign(tmp_path, extra=""):
    md = tmp_path / "c.md"
    md.write_text(
        "```yaml\ncampaign_id: x\ngoal: lead\nthreshold: 0.7\n"
        f"escalate_band: [0.4, 0.75]\n{extra}```\n"
        "## Relevance\nr\n## Match\nm\n## Extract\ne\n", encoding="utf-8")
    return md


def test_platform_parses_and_normalizes(tmp_path):
    c = load_campaign(_thin_campaign(tmp_path, "platform: YouTube\n"))
    assert c.platform == "youtube"   # lower-cased


def test_telegram_seed_channels_parse(tmp_path):
    c = load_campaign(_thin_campaign(
        tmp_path, "platform: telegram\nseed_channels: [product_updates, saas_chat]\n"))
    assert c.platform == "telegram"
    assert c.seed_channels == ["product_updates", "saas_chat"]


def test_unsupported_platform_rejected(tmp_path):
    import pytest
    with pytest.raises(ValueError, match="not supported"):
        load_campaign(_thin_campaign(tmp_path, "platform: tiktok\n"))


def test_campaign_from_brief_builds_runnable_campaign():
    brief = {
        "platform": "youtube", "goal": "lead", "threshold": 0.8,
        "language_mix": ["uz", "ru"],
        "relevance_def": "saas product", "match_def": "buyer intent",
        "extract_def": "- phone", "seed_channels": ["UC_abc"],
        "seed_hashtags": ["projectmanagement"],
    }
    c = campaign_from_brief("yt-leadgen", brief)
    assert c.campaign_id == "yt-leadgen"
    assert c.platform == "youtube" and c.threshold == 0.8
    assert c.language_mix == ["uz", "ru"]
    assert c.relevance_def == "saas product" and c.match_def == "buyer intent"
    assert c.seed_channels == ["UC_abc"] and c.seed_hashtags == ["projectmanagement"]


def test_campaign_from_brief_defaults_and_validation():
    c = campaign_from_brief("c", {})            # empty brief → safe defaults
    assert c.platform == "instagram" and c.threshold == 0.70
    assert c.escalate_band == (0.4, 0.75)
    with pytest.raises(ValueError, match="not supported"):
        campaign_from_brief("c", {"platform": "tiktok"})
    with pytest.raises(ValueError, match="campaign_id"):
        campaign_from_brief("", {})


def test_include_home_feed_off_when_seeds_present():
    # The shipped campaign defines its own seeds, so it must NOT also drag in the
    # warmed home feed — that is what leaked off-campaign reels.
    c = load_campaign(CONFIG / "campaign.md")
    assert c.seed_hashtags          # precondition: it has seeds
    assert c.include_home_feed is False


def test_include_home_feed_on_when_no_seeds(tmp_path):
    c = load_campaign(_thin_campaign(tmp_path))   # thin campaign has no seeds
    assert c.seed_hashtags == [] and c.seed_accounts == []
    assert c.include_home_feed is True


def test_include_home_feed_explicit_override(tmp_path):
    # Seeds present but the operator opts the home feed back IN.
    on = load_campaign(_thin_campaign(
        tmp_path, "seed_hashtags: [flutterdev]\ninclude_home_feed: true\n"))
    assert on.seed_hashtags == ["flutterdev"] and on.include_home_feed is True
    # No seeds but the operator opts the home feed OUT.
    off = load_campaign(_thin_campaign(tmp_path, "include_home_feed: false\n"))
    assert off.include_home_feed is False


def test_brief_include_home_feed_seed_aware_default_and_override():
    # Seeded brief → home feed OFF by default (hunt the seeds, not a warmed feed).
    assert campaign_from_brief("x", {"seed_hashtags": ["flutterdev"]}).include_home_feed is False
    # Explicit override wins even with seeds.
    assert campaign_from_brief(
        "x", {"seed_hashtags": ["flutterdev"], "include_home_feed": True}).include_home_feed is True
    # Seedless brief → home feed ON.
    assert campaign_from_brief("x", {}).include_home_feed is True


def test_parse_extract_fields_backticked_with_gloss():
    # The shipped form: `- \`key\` — human description`.
    ed = ("- `phone` — any phone number in the comment, normalized, else null.\n"
          "- `intent` — short tag: buy | trial | demo.\n"
          "- `company` — the commenter's company or team, else null.")
    assert parse_extract_fields(ed) == ["phone", "intent", "company"]


def test_parse_extract_fields_plain_and_multiword():
    # Loose, no backticks — a multi-word bullet becomes ONE snake_case key
    # (the old parser truncated `- first name and last name` to just `first`).
    ed = "- phone\n- username\n- first name and last name"
    assert parse_extract_fields(ed) == ["phone", "username", "first_name_and_last_name"]


def test_parse_extract_fields_comma_shorthand_and_dedupe():
    # No bullets, single-word comma list → one field per token; dupes collapse.
    assert parse_extract_fields("phone, email, phone") == ["phone", "email"]


def test_parse_extract_fields_prose_line_stays_one_field():
    # A comma-separated PROSE description must not fragment into phantom keys.
    ed = "full mailing address, including street, city and postcode"
    assert parse_extract_fields(ed) == ["full_mailing_address_including_street_city_and_postcode"]


def test_parse_extract_fields_numbered_list():
    # Ordered markdown lists are bullets too — the number is not part of the key.
    assert parse_extract_fields("1. phone\n2. email\n3) budget") == ["phone", "email", "budget"]


def test_parse_extract_fields_unicode_keys_survive():
    # The engine targets uz/ru briefs: Cyrillic keys must NOT be silently dropped,
    # and accents are folded rather than shredding the name into fragments.
    assert parse_extract_fields("- телефон\n- бюджет") == ["телефон", "бюджет"]
    assert parse_extract_fields("- `телефон` — phone") == ["телефон"]
    assert parse_extract_fields("- téléphone\n- prénom") == ["telephone", "prenom"]


def test_parse_extract_fields_empty_is_no_constraint():
    # Blank section ⇒ no declared fields ⇒ cascade leaves extraction unconstrained.
    assert parse_extract_fields("") == []
    assert parse_extract_fields("   \n  ") == []


def test_campaign_extract_fields_matches_shipped_schema():
    c = load_campaign(CONFIG / "campaign.md")
    assert c.extract_fields() == ["phone", "email", "intent"]


def test_campaign_to_brief_round_trips_file_campaign():
    """A Campaign → full brief blob captures the YAML-only knobs the panel form
    can't carry, and round-trips back losslessly (used to seed the merge base when
    editing the file-backed primary campaign so those knobs aren't dropped)."""
    c = load_campaign(CONFIG / "campaign.md")
    brief = campaign_to_brief(c)
    # the knobs the Create/Edit form cannot set are present…
    assert brief["enable_actions"] is False   # harvest read-only (warming PRD §4.3)
    assert brief["escalate_band"] == [0.4, 0.75]
    assert brief["seed_direction"]
    assert brief["match_prompt"]
    # …and rebuilding from the blob reproduces the campaign's behaviour.
    c2 = campaign_from_brief(c.campaign_id, brief)
    assert c2.enable_actions == c.enable_actions
    assert c2.escalate_band == c.escalate_band
    assert c2.max_likes_per_session == c.max_likes_per_session
    assert c2.match_prompt == c.match_prompt
    assert c2.extract_def == c.extract_def
    assert c2.seed_direction == c.seed_direction


# --- Multi-platform channels (Phase 1) ---------------------------------------


def test_resolve_home_feed_off_when_only_seed_channels():
    # Telegram-style seeds (channels only) must also turn the home feed OFF — the
    # seed-aware default now counts seed_channels alongside hashtags/accounts.
    assert _resolve_home_feed({}, [], [], ["product_updates"]) is False
    assert _resolve_home_feed({}, [], [], []) is True


def test_channel_spec_from_dict_valid():
    spec = _channel_spec_from_dict({
        "platform": "YouTube", "seed_hashtags": ["saas"],
        "seed_accounts": ["UC_abc"], "seed_channels": ["chan"]})
    assert spec == ChannelSpec(
        platform="youtube", seed_hashtags=("saas",),
        seed_accounts=("UC_abc",), seed_channels=("chan",),
        include_home_feed=False)            # has seeds → home feed OFF by default


def test_channel_spec_from_dict_invalid_platform_returns_none():
    assert _channel_spec_from_dict({"platform": "tiktok"}) is None
    assert _channel_spec_from_dict({"platform": ""}) is None
    assert _channel_spec_from_dict("not-a-dict") is None


def test_channel_spec_from_dict_explicit_home_feed_override():
    # No seeds would default home feed ON; an explicit false must win.
    spec = _channel_spec_from_dict({"platform": "instagram", "include_home_feed": False})
    assert spec.platform == "instagram" and spec.include_home_feed is False


def test_campaign_from_brief_multi_channel_round_trip():
    brief = {"platform": "instagram", "channels": [
        {"platform": "instagram", "seed_hashtags": ["a"]},
        {"platform": "youtube", "seed_channels": ["UC1"]}]}
    c = campaign_from_brief("multi", brief)
    assert len(c.channels) == 2
    assert c.channels[0] == ChannelSpec(platform="instagram", seed_hashtags=("a",),
                                        include_home_feed=False)
    assert c.channels[1] == ChannelSpec(platform="youtube", seed_channels=("UC1",),
                                        include_home_feed=False)


def test_campaign_from_brief_channels_absent_yields_empty_tuple():
    assert campaign_from_brief("c", {}).channels == ()
    # A non-list channels value is tolerated, not fatal.
    assert campaign_from_brief("c", {"channels": "oops"}).channels == ()


def test_campaign_from_brief_invalid_platform_in_channels_dropped():
    brief = {"channels": [
        {"platform": "instagram"}, {"platform": "tiktok"}, "junk"]}
    c = campaign_from_brief("c", brief)
    assert [ch.platform for ch in c.channels] == ["instagram"]


def test_campaign_to_brief_omits_channels_for_zero_channels():
    c = campaign_from_brief("c", {"platform": "instagram"})
    assert "channels" not in campaign_to_brief(c)


def test_campaign_to_brief_omits_channels_for_single_channel():
    # M4: one channel collapses to flat scalars sourced FROM that channel.
    c = campaign_from_brief("c", {"channels": [
        {"platform": "youtube", "seed_channels": ["UC1"], "include_home_feed": False}]})
    brief = campaign_to_brief(c)
    assert "channels" not in brief
    assert brief["platform"] == "youtube"
    assert brief["seed_channels"] == ["UC1"]
    assert brief["include_home_feed"] is False


# ---- share_target (warming-writes PRD §6.2, O-share-target) ----

def test_share_target_defaults_to_none():
    assert campaign_from_brief("c", {}).share_target is None
    assert load_campaign(CONFIG / "campaign.md").share_target is None


def test_share_target_round_trips_through_brief():
    c = campaign_from_brief("c", {"share_target": "@warming_safe"})
    assert c.share_target == "warming_safe"            # leading '@' stripped
    assert campaign_to_brief(c)["share_target"] == "warming_safe"


def test_share_target_from_brief_strips_at_and_normalizes():
    assert campaign_from_brief("c", {"share_target": "plain_handle"}).share_target == "plain_handle"
    # Blank / whitespace-only collapses to None (the live-resolution default).
    assert campaign_from_brief("c", {"share_target": "   "}).share_target is None
    assert campaign_from_brief("c", {"share_target": ""}).share_target is None


def test_share_target_rejects_invalid_handle():
    with pytest.raises(ValueError):
        campaign_from_brief("c", {"share_target": "not a handle!"})
    with pytest.raises(ValueError):
        campaign_from_brief("c", {"share_target": "x" * 31})


def test_share_target_loads_from_yaml_block(tmp_path):
    md = tmp_path / "campaign.md"
    md.write_text(
        "```yaml\ncampaign_id: t\nshare_target: '@dm_target'\n```\n",
        encoding="utf-8")
    assert load_campaign(md).share_target == "dm_target"


def test_share_target_to_brief_is_none_when_unset():
    c = campaign_from_brief("c", {"platform": "instagram"})
    assert campaign_to_brief(c)["share_target"] is None


def test_campaign_to_brief_emits_channels_for_two_or_more():
    c = campaign_from_brief("c", {"channels": [
        {"platform": "instagram", "seed_hashtags": ["a"]},
        {"platform": "youtube", "seed_channels": ["UC1"]}]})
    brief = campaign_to_brief(c)
    assert [ch["platform"] for ch in brief["channels"]] == ["instagram", "youtube"]
    assert brief["channels"][0]["seed_hashtags"] == ["a"]


def test_campaign_to_brief_channels_are_json_serializable():
    import json
    c = campaign_from_brief("c", {"channels": [
        {"platform": "instagram"}, {"platform": "x"}]})
    # No tuples leak through: json.dumps would raise on a non-serializable value.
    round_tripped = json.loads(json.dumps(campaign_to_brief(c)))
    assert isinstance(round_tripped["channels"][0]["seed_hashtags"], list)


def test_soul_loads():
    s = load_soul(CONFIG / "soul.md")
    assert "Read-only" in s.text
