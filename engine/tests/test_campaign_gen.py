"""Unit tests for AI-first campaign generation: the SSRF guard, HTML→text,
the Playwright→httpx fetch fallback, draft coercion, and the never-crash
robustness chain (tolerant parse → coerce → validate → retry → typed error)."""
import socket

import pytest

from reelradar import campaign_gen as cg
from reelradar.campaign_gen import (CampaignGenError, ProductContext,
                                     UrlFetchError, _PlaywrightUnavailable,
                                     _assert_url_safe, _html_to_text,
                                     assemble_draft, assemble_interview,
                                     generate_advanced_prompts,
                                     generate_campaign, run_interview)

# A classifier prompt that keeps the required JSON output contract.
_VALID_PROMPT = (
    "You classify whether a post is about running shoes. Judge by meaning in any "
    'language. Output ONLY one minified JSON object: {"label":str,"score":0..1,'
    '"confidence":0..1,"reason":str,"extracted":{}}.')

# A resolver stub that always reports a public address (no real DNS).
_PUBLIC = lambda host, port: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]


# ----- a stub router (no network) -----

GOOD_BRIEF = {
    "name": "Acme Running Shoes", "platform": "instagram", "objective": "lead",
    "relevanceDef": "Reels about running shoes and marathon training.",
    "matchDef": "A commenter who wants to buy running shoes or asks the price.",
    "extractDef": "- phone — contact number", "seedHashtags": ["running", "marathon"],
    "seedAccounts": ["acme.run"], "languageMix": ["en"], "threshold": 0.7,
}


class _StubRouter:
    text_model = "stub-text"
    vision_model = "stub-vision"

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def generate_json(self, *, system, user, images_b64=None, model=None, **kw):
        self.calls.append({"user": user, "images_b64": images_b64, "model": model})
        return self._responses.pop(0) if self._responses else {}


# ----- SSRF guard -----

@pytest.mark.parametrize("url", [
    "http://127.0.0.1/admin",          # loopback
    "http://10.1.2.3/",                # private
    "http://192.168.0.1/",             # private
    "http://169.254.169.254/latest",   # cloud metadata (link-local)
    "http://[::1]/",                   # IPv6 loopback
    "http://0.0.0.0/",                 # unspecified
    "file:///etc/passwd",              # non-http scheme
    "gopher://example.com/",           # non-http scheme
])
def test_assert_url_safe_rejects_dangerous(url):
    with pytest.raises(UrlFetchError):
        _assert_url_safe(url)


def test_assert_url_safe_allows_public_literal_ip():
    _assert_url_safe("http://8.8.8.8/")  # numeric, public — no raise


def test_assert_url_safe_blocks_dns_rebind_to_private():
    rebind = lambda host, port: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0))]
    with pytest.raises(UrlFetchError):
        _assert_url_safe("https://evil.example.com/", rebind)


def test_assert_url_safe_allows_public_hostname():
    _assert_url_safe("https://example.com/page", _PUBLIC)  # no raise


# ----- HTML → text -----

def test_html_to_text_extracts_title_and_drops_script_style():
    html = ("<html><head><title>Buy Shoes</title>"
            "<meta name='description' content='Best running shoes'></head>"
            "<body><script>track()</script><p>Marathon shoes for sale</p>"
            "<style>.x{color:red}</style></body></html>")
    title, text = _html_to_text(html)
    assert title == "Buy Shoes"
    assert "Best running shoes" in text
    assert "Marathon shoes for sale" in text
    assert "track()" not in text and ".x{" not in text


def test_html_to_text_tolerates_broken_markup():
    title, text = _html_to_text("<p>hi <b>there")  # never raises
    assert "hi" in text and "there" in text


# ----- httpx fetch path (injected fake client; no network) -----

class _FakeResp:
    def __init__(self, *, is_redirect=False, headers=None, body=b"", encoding="utf-8"):
        self.is_redirect = is_redirect
        self.headers = headers or {}
        self._body = body
        self.encoding = encoding

    def iter_bytes(self):
        yield self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requested = []
        self.closed = False

    def stream(self, method, url):
        self.requested.append(url)
        return self._responses.pop(0)

    def close(self):
        self.closed = True


def test_fetch_via_httpx_returns_page_text():
    client = _FakeClient([_FakeResp(headers={"content-type": "text/html"},
                                    body=b"<title>T</title><body><p>Hello world</p></body>")])
    title, text = cg._fetch_via_httpx("http://93.184.216.34/", socket.getaddrinfo,
                                      open_client=lambda: client)
    assert title == "T" and "Hello world" in text
    assert client.closed is True


def test_fetch_via_httpx_revalidates_redirect_to_private():
    client = _FakeClient([_FakeResp(is_redirect=True,
                                    headers={"location": "http://127.0.0.1/"})])
    with pytest.raises(UrlFetchError):
        cg._fetch_via_httpx("http://93.184.216.34/", socket.getaddrinfo,
                            open_client=lambda: client)


def test_fetch_via_httpx_rejects_non_html_content():
    client = _FakeClient([_FakeResp(headers={"content-type": "application/pdf"}, body=b"%PDF")])
    with pytest.raises(UrlFetchError):
        cg._fetch_via_httpx("http://93.184.216.34/", socket.getaddrinfo,
                            open_client=lambda: client)


# ----- fetch_rendered_text fallback selection -----

def test_fetch_rendered_text_falls_back_when_playwright_unavailable(monkeypatch):
    monkeypatch.setattr(cg, "_fetch_via_playwright",
                        lambda url, res: (_ for _ in ()).throw(_PlaywrightUnavailable("no browser")))
    monkeypatch.setattr(cg, "_fetch_via_httpx", lambda url, res: ("FELL", "back text"))
    title, text = cg.fetch_rendered_text("http://93.184.216.34/", socket.getaddrinfo)
    assert title == "FELL" and text == "back text"


def test_fetch_rendered_text_does_not_fall_back_on_ssrf(monkeypatch):
    monkeypatch.setattr(cg, "_fetch_via_playwright",
                        lambda url, res: (_ for _ in ()).throw(UrlFetchError("blocked")))
    monkeypatch.setattr(cg, "_fetch_via_httpx",
                        lambda url, res: pytest.fail("must not fetch a blocked target"))
    with pytest.raises(UrlFetchError):
        cg.fetch_rendered_text("http://93.184.216.34/", socket.getaddrinfo)


# ----- draft coercion -----

def test_assemble_draft_coerces_and_defaults():
    raw = {"name": "X", "platform": "facebook", "objective": "growth",
           "threshold": 5, "seedHashtags": ["a", "b"], "languageMix": ["en", "ru"],
           "extractDef": ["phone", "email"]}
    draft = assemble_draft(raw, ProductContext())
    assert draft["platform"] == "instagram"      # unknown platform → default
    assert draft["objective"] == "lead"           # unknown objective → default
    assert draft["threshold"] == 1.0              # clamped into [0,1]
    assert draft["seedHashtags"] == "a, b"        # list → comma string
    assert draft["languages"] == "en, ru"
    assert draft["extractDef"] == "- phone\n- email"   # bulletized
    assert draft["budgetCap"] == 7500 and draft["goalTarget"] == 200
    assert draft["seedChannels"] == ""            # blank for non-telegram


def test_assemble_draft_names_from_context_when_model_omits_it():
    draft = assemble_draft({}, ProductContext(page_title="Acme Analytics"))
    assert draft["name"] == "Acme Analytics"


def test_assemble_draft_keeps_telegram_channels():
    draft = assemble_draft({"platform": "telegram", "seedChannels": ["@a", "@b"]},
                           ProductContext())
    assert draft["seedChannels"] == "@a, @b"


# ----- generate_campaign robustness chain -----

def test_generate_campaign_text_only_returns_flat_draft():
    router = _StubRouter([GOOD_BRIEF])
    draft = generate_campaign(text="We sell running shoes", router=router)
    assert draft["name"] == "Acme Running Shoes"
    assert draft["seedHashtags"] == "running, marathon"   # comma string contract
    assert set(draft) >= {"name", "objective", "platform", "relevanceDef",
                          "matchDef", "extractDef", "seedHashtags", "seedChannels"}
    # synthesis + the (best-effort) advanced-prompts call
    assert len(router.calls) == 2


def test_generate_campaign_retries_once_on_unusable_output():
    router = _StubRouter([{}, GOOD_BRIEF])     # first reply garbage, retry good
    draft = generate_campaign(text="running shoes", router=router)
    assert draft["matchDef"]
    # garbage synthesis + strict retry + the advanced-prompts call
    assert len(router.calls) == 3
    assert router.calls[1]["user"].startswith("Your previous answer")   # strict retry


def test_generate_campaign_raises_when_model_never_usable():
    router = _StubRouter([])    # always {} → never usable
    with pytest.raises(CampaignGenError):
        generate_campaign(text="running shoes", router=router)


def test_generate_campaign_uses_image_caption():
    router = _StubRouter([{"caption": "An online store selling trail shoes"}, GOOD_BRIEF])
    draft = generate_campaign(image_b64="aGVsbG8=", router=router)
    assert router.calls[0]["images_b64"] == ["aGVsbG8="]     # captioning call
    assert "PRODUCT SCREENSHOT" in router.calls[1]["user"]   # caption fed to synthesis
    assert draft["relevanceDef"]


def test_assemble_draft_appends_supply_side_exclusion_when_model_omits_it():
    """A lead campaign whose model-written matchDef forgets the seller exclusion
    gets the canonical clause appended at the boundary (don't trust the model)."""
    raw = {"name": "X", "objective": "lead",
           "matchDef": "A commenter who wants to buy the product or asks the price."}
    draft = assemble_draft(raw, ProductContext())
    low = draft["matchDef"].lower()
    assert "not a lead" in low and "offering or selling" in low
    # the model's original sentence is preserved, the clause is added after it
    assert draft["matchDef"].startswith("A commenter who wants to buy")


def test_assemble_draft_does_not_duplicate_supply_side_exclusion():
    """When the matchDef already excludes the supply side, nothing is appended."""
    md = ("A commenter who wants to buy. Exclude the supply side: anyone offering "
          "or selling is not a lead.")
    draft = assemble_draft({"name": "X", "objective": "lead", "matchDef": md},
                           ProductContext())
    assert draft["matchDef"] == md          # unchanged, no second clause
    assert draft["matchDef"].lower().count("not a lead") == 1


def test_assemble_draft_skips_supply_side_clause_for_non_lead_objective():
    """Awareness/traffic campaigns aren't buyer-acquisition, so leave matchDef as-is."""
    raw = {"name": "X", "objective": "awareness", "matchDef": "Anyone engaging."}
    draft = assemble_draft(raw, ProductContext())
    assert draft["matchDef"] == "Anyone engaging."


def test_campaign_gen_prompt_requires_supply_side_exclusion():
    """The generator must instruct that matchDef excludes SELLERS / supply-side.

    Without this, AI-generated buyer campaigns produce a matchDef that qualifies a
    commenter offering their OWN product or service for sale as a lead — demand and
    supply look identical to the domain-free scorer. Lock the instruction so the
    blind spot can't return.
    """
    p = cg.SYSTEM_CAMPAIGN_GEN.lower()
    assert "demand-side only" in p
    assert "supply side is not a lead" in p
    assert "offering or selling" in p


def test_generate_campaign_raises_on_empty_context():
    with pytest.raises(CampaignGenError):
        generate_campaign(router=_StubRouter([]))    # no url/image/text


# ----- interview coercion (assemble_interview) -----

def test_assemble_interview_done_returns_no_questions():
    done, questions = assemble_interview(
        {"done": True, "questions": [{"id": "x", "type": "text", "prompt": "ignored"}]},
        round=2)
    assert done is True and questions == []


def test_assemble_interview_non_dict_is_tolerated():
    done, questions = assemble_interview("not json", round=2)  # never raises
    assert done is False and questions == []


def test_assemble_interview_injects_platforms_question_on_first_round():
    done, questions = assemble_interview(
        {"done": False, "questions": [
            {"id": "buyer", "type": "text", "prompt": "Who is your ideal buyer?"}]},
        round=1)
    assert done is False
    assert any(q["type"] == "platforms" for q in questions)
    plat = next(q for q in questions if q["type"] == "platforms")
    assert plat["suggested"] == ["instagram"]   # default when none supplied


def test_assemble_interview_keeps_model_platforms_and_filters_suggested():
    done, questions = assemble_interview(
        {"questions": [{"id": "p", "type": "platforms", "prompt": "Where?",
                        "suggested": ["instagram", "facebook", "x", "x"]}]},
        round=1)
    plat = next(q for q in questions if q["type"] == "platforms")
    assert plat["suggested"] == ["instagram", "x"]   # unknown dropped, deduped


def test_assemble_interview_drops_choice_question_without_options():
    done, questions = assemble_interview(
        {"questions": [
            {"id": "a", "type": "single", "prompt": "Pick", "options": []},
            {"id": "b", "type": "single", "prompt": "Goal",
             "options": [{"value": "lead", "label": "Leads"}]}]},
        round=2)
    ids = {q["id"] for q in questions}
    assert "a" not in ids and "b" in ids


def test_assemble_interview_clamps_question_count():
    qs = [{"id": f"q{i}", "type": "text", "prompt": f"Q{i}"} for i in range(9)]
    _, questions = assemble_interview({"questions": qs}, round=2)
    assert len(questions) <= 4


def test_assemble_interview_drops_already_asked_questions():
    """The flaky model loves to re-ask; a question already asked in a prior round
    (passed via `asked`) is dropped so the interview makes progress."""
    raw = {"questions": [
        {"id": "a", "type": "text", "prompt": "Who is your ideal buyer?"},
        {"id": "b", "type": "text", "prompt": "What is your budget?"}]}
    _, questions = assemble_interview(
        raw, round=2, asked=("who is your IDEAL buyer?",))  # case/space-insensitive
    prompts = [q["prompt"] for q in questions]
    assert "Who is your ideal buyer?" not in prompts
    assert "What is your budget?" in prompts


def test_assemble_interview_drops_within_round_duplicates():
    raw = {"questions": [
        {"id": "a", "type": "text", "prompt": "Who buys this?"},
        {"id": "b", "type": "text", "prompt": "Who buys this?"}]}
    _, questions = assemble_interview(raw, round=2)
    assert len(questions) == 1


def test_run_interview_converges_when_model_only_repeats():
    """If every question the model returns was already asked, the round dedups to
    empty and run_interview converges to done (→ synthesis) instead of looping."""
    repeat = {"done": False, "questions": [
        {"id": "x", "type": "text", "prompt": "Who is your ideal buyer?"}]}
    router = _StubRouter([repeat, repeat])  # initial + strict retry both repeat
    result = run_interview(product_context="ctx", round=2,
                           interview=[{"question": "Who is your ideal buyer?",
                                       "answer": "runners"}], router=router)
    assert result.done is True and result.questions == []


def test_assemble_interview_dedupes_platforms_questions():
    _, questions = assemble_interview(
        {"questions": [
            {"type": "platforms", "prompt": "A", "suggested": ["x"]},
            {"type": "platforms", "prompt": "B", "suggested": ["reddit"]}]},
        round=2)
    assert sum(1 for q in questions if q["type"] == "platforms") == 1


# ----- run_interview -----

def test_run_interview_first_round_returns_questions_and_context():
    router = _StubRouter([{"done": False, "questions": [
        {"id": "buyer", "type": "text", "prompt": "Who is your ideal buyer?"}]}])
    result = run_interview(text="We sell running shoes", router=router, round=1)
    assert result.done is False
    assert result.product_context  # serialized, echoed back to the panel
    assert any(q["type"] == "platforms" for q in result.questions)


def test_run_interview_reuses_product_context_without_refetch():
    router = _StubRouter([{"done": False, "questions": [
        {"id": "x", "type": "text", "prompt": "More?"}]}])
    result = run_interview(product_context="PRODUCT DESCRIPTION:\nrunning shoes",
                           interview=[{"question": "Who?", "answer": "runners"}],
                           router=router, round=2)
    # round 2 carries no forced platforms question; context flows through verbatim
    assert "running shoes" in result.product_context
    assert "PRODUCT DESCRIPTION" in router.calls[0]["user"]


def test_run_interview_caps_rounds():
    router = _StubRouter([{"done": False, "questions": [
        {"id": "x", "type": "text", "prompt": "more"}]}])
    result = run_interview(product_context="ctx", router=router,
                           round=cg.MAX_INTERVIEW_ROUNDS + 1)
    assert result.done is True and result.questions == []
    assert router.calls == []   # past the cap: no model call


def test_run_interview_forces_done_when_model_never_usable():
    router = _StubRouter([{}, {}])   # two unusable replies (no questions, not done)
    result = run_interview(product_context="ctx", router=router, round=2)
    assert result.done is True   # never dead-ends the wizard
    assert len(router.calls) == 2   # initial + one strict retry


def test_run_interview_raises_on_empty_context():
    with pytest.raises(CampaignGenError):
        run_interview(product_context="", router=_StubRouter([]), round=1)


def test_run_interview_surfaces_dead_model_on_first_round():
    """A removed/404 model makes generate_json return {} — round 1 must surface a
    real error, not fake a platform-only interview (the owl-alpha regression)."""
    router = _StubRouter([{}, {}])   # initial + strict retry both empty (model down)
    with pytest.raises(CampaignGenError):
        run_interview(product_context="ctx", router=router, round=1)


# ----- generate_campaign with interview + chosen platforms -----

def test_generate_campaign_uses_interview_and_builds_channels():
    router = _StubRouter([GOOD_BRIEF])
    draft = generate_campaign(
        product_context="PRODUCT DESCRIPTION:\nrunning shoes",
        interview=[{"question": "Goal?", "answer": "find buyers"}],
        platforms=["x", "instagram"], router=router)
    assert draft["platform"] == "x"   # first chosen platform is primary
    assert "channels" in draft and len(draft["channels"]) == 2
    assert draft["channels"][0]["platform"] == "x"
    # the model's seeds land on the primary channel only
    assert draft["channels"][0]["seedHashtags"] == "running, marathon"
    assert draft["channels"][1]["seedHashtags"] == ""
    # interview transcript reached the synthesis prompt
    assert "CLIENT INTERVIEW" in router.calls[0]["user"]
    assert "running shoes" in router.calls[0]["user"]


def test_generate_campaign_fills_defs_when_model_fails_but_interview_present():
    """A flaky model returns nothing usable, but the client answered the interview —
    we fill the missing defs from their answers rather than dead-ending with a 422."""
    router = _StubRouter([{}, {}])  # initial + strict retry both empty
    draft = generate_campaign(
        product_context="PRODUCT DESCRIPTION:\nAcme task tracker for startups",
        interview=[{"question": "Who is your ideal buyer?",
                    "answer": "startup founders evaluating PM tools"}],
        platforms=["instagram"], router=router)
    assert draft["relevanceDef"]   # filled from the gathered signal
    assert draft["matchDef"]
    assert "not a lead" in draft["matchDef"].lower()   # supply-side clause still applied
    assert "task tracker" in draft["relevanceDef"].lower()   # topic from context


def test_generate_campaign_still_raises_without_interview_signal():
    """Plain generate (no interview/platforms) keeps the strict raise-on-unusable
    contract — we only fall back when the user invested in the interview."""
    router = _StubRouter([])  # always {} → never usable
    with pytest.raises(CampaignGenError):
        generate_campaign(text="running shoes", router=router)


def test_generate_campaign_single_platform_has_no_channels():
    router = _StubRouter([GOOD_BRIEF])
    draft = generate_campaign(product_context="ctx", platforms=["youtube"],
                              router=router)
    assert draft["platform"] == "youtube"
    assert "channels" not in draft   # single platform stays flat


# ----- advanced (tuned) classifier prompt generation -----

def test_generate_campaign_attaches_advanced_prompts():
    prompts = {"relevancePrompt": _VALID_PROMPT, "matchPrompt": _VALID_PROMPT,
               "visionPrompt": _VALID_PROMPT}
    router = _StubRouter([GOOD_BRIEF, prompts])   # synthesis, then prompts call
    draft = generate_campaign(text="running shoes", router=router)
    assert draft["relevancePrompt"] == _VALID_PROMPT
    assert draft["matchPrompt"] == _VALID_PROMPT
    assert draft["visionPrompt"] == _VALID_PROMPT


def test_generate_campaign_drops_prompts_without_json_contract():
    """A generated prompt that loses the JSON output contract is discarded so the
    engine falls back to its tuned generic prompt (blank = use default)."""
    bad = {"relevancePrompt": "Just classify the vibes, nothing structured here folks "
                              "and definitely no output spec whatsoever at all.",
           "matchPrompt": "", "visionPrompt": "short"}
    router = _StubRouter([GOOD_BRIEF, bad])
    draft = generate_campaign(text="running shoes", router=router)
    assert draft["relevancePrompt"] == ""   # left blank → engine generic prompt
    assert draft["matchPrompt"] == ""
    assert draft["visionPrompt"] == ""


def test_generate_advanced_prompts_keeps_only_contract_carrying():
    mixed = {"relevancePrompt": _VALID_PROMPT,
             "matchPrompt": "long enough text but it lacks every required marker here",
             "visionPrompt": _VALID_PROMPT}
    out = generate_advanced_prompts(
        {"relevanceDef": "x", "matchDef": "y", "extractDef": "- z"},
        _StubRouter([mixed]), model=None)
    assert set(out) == {"relevancePrompt", "visionPrompt"}


def test_generate_advanced_prompts_tolerates_garbage():
    out = generate_advanced_prompts({"relevanceDef": "x"}, _StubRouter([]), model=None)
    assert out == {}   # empty model reply → no prompts, never raises
