"""Quick setup builds a safe, ready-to-test agent from ticked goals."""
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import quick_setup as mod
from app.account import current_user
from app.main import app

client = TestClient(app)
W = "00000000-0000-0000-0000-00000000000a"
URL = "/api/account/organizations/%s/quick-setup" % W


class Env:
    def __init__(self, calendar=False, products=False, assistant=True, provision_ok=True, sync_ok=True):
        self.calendar, self.products, self.assistant, self.provision_ok, self.sync_ok = calendar, products, assistant, provision_ok, sync_ok
        self.saved, self.patched, self.provisioned, self.synced = [], [], 0, 0

    def api(self, table, method="GET", params=None, data=None, **kw):
        if table == "agent_configs" and method == "GET":
            return [{"agent_name": "Alex", "provider_agent_id": "asst" if self.assistant else None, "transfer_directory": []}]
        if table == "agent_configs" and method == "PATCH":
            self.patched.append(data)
        if table == "catalog_items":
            return [{"id": "1"}] if self.products else []
        return []


@pytest.fixture
def make(monkeypatch):
    def build(**kw):
        env = Env(**kw)
        monkeypatch.setattr(mod, "api", env.api)
        monkeypatch.setattr(mod, "membership", lambda *a, **k: "owner")
        monkeypatch.setattr(mod, "org_or_404", lambda w: {"name": "Metro Print", "industry": "Printing"})
        monkeypatch.setattr(mod, "save_agent_configuration", lambda w, edit: env.saved.append(edit))
        monkeypatch.setattr("app.calendar.connection", lambda w: {"id": 1} if env.calendar else None)
        def provision(w):
            if not env.provision_ok:
                raise HTTPException(503, "no key")
            env.assistant = True
            env.provisioned += 1
            return {"assistant_id": "asst"}
        monkeypatch.setattr("app.cloud.provision_test_agent", provision)
        def refresh(w, **k):
            env.synced += 1
            return {"reason": "voice_policy_and_one_second_opening_synced" if env.sync_ok else "not_managed_assistant"}
        monkeypatch.setattr("app.call_intel.refresh_receptionist_prompt", refresh)
        app.dependency_overrides[current_user] = lambda: {"id": "u1"}
        return env
    yield build
    app.dependency_overrides.clear()


def test_compose_identifies_the_ai_names_the_business_and_stays_short():
    greeting, text = mod.compose("Metro Print", "Printing", "Alex", ["info", "quotes", "orders"], "Ask about deadlines first.")
    assert "Metro Print" in greeting and "AI receptionist" in greeting and 10 <= len(greeting) <= 500
    assert "quote" in text and "order" in text.lower() and "Ask about deadlines first." in text and len(text) <= 2400
    assert "book" not in text.lower().replace("booking", "")


def test_goals_without_prerequisites_enable_only_what_is_safe_and_list_what_is_missing(make):
    env = make()
    result = client.post(URL, json={"goals": ["info", "messages", "quotes", "orders", "booking"]}).json()
    assert env.patched[0] == {"voice_leads_enabled": True, "voice_booking_enabled": False,
                              "voice_catalog_enabled": False, "voice_order_requests_enabled": False}
    assert {n["key"] for n in result["needs"]} == {"calendar", "products"}
    assert "Book appointments" not in result["enabled"] and "Take orders" not in result["enabled"]
    assert "Answer questions" in result["enabled"] and result["synced"] is True and len(env.saved) == 1


def test_prerequisites_met_turns_everything_on(make):
    env = make(calendar=True, products=True)
    result = client.post(URL, json={"goals": ["booking", "orders"]}).json()
    assert env.patched[0]["voice_booking_enabled"] and env.patched[0]["voice_order_requests_enabled"] and env.patched[0]["voice_catalog_enabled"]
    assert result["needs"] == []


def test_missing_assistant_is_created_once_and_synced(make):
    env = make(assistant=False)
    result = client.post(URL, json={"goals": ["info"]}).json()
    assert env.provisioned == 1 and env.synced == 1 and result["assistant_ready"] is True


def test_provision_failure_and_sync_failure_are_reported_not_hidden(make):
    make(assistant=False, provision_ok=False)
    result = client.post(URL, json={"goals": ["info"]}).json()
    assert result["assistant_ready"] is False and any(n["key"] == "assistant" for n in result["needs"])
    make(sync_ok=False)
    result = client.post(URL, json={"goals": ["info"]}).json()
    assert result["synced"] is False and any(n["key"] == "sync" for n in result["needs"])


def test_transfer_number_uses_the_shared_validation_and_atomic_save(make, monkeypatch):
    env = make()
    saved = []
    monkeypatch.setattr("app.transfer_directory.reject_loops", lambda w, d: None)
    monkeypatch.setattr("app.transfer_directory.rpc", lambda name, payload: saved.append((name, payload)) or True)
    client.post(URL, json={"goals": ["transfer"], "transfer_number": "+12125550100", "transfer_label": "Front desk"})
    assert saved[0][0] == "save_workspace_transfer_directory" and saved[0][1]["p_previous"] == []
    assert saved[0][1]["p_directory"][0]["number"] == "+12125550100" and "transfer_directory" not in env.patched[0]
    env = make()
    result = client.post(URL, json={"goals": ["transfer"]}).json()
    assert any(n["key"] == "transfer" for n in result["needs"]) and "transfer_directory" not in env.patched[0]


@pytest.mark.parametrize("number", ["+19005550100", "+12129765550"])
def test_premium_rate_transfer_numbers_are_refused_like_the_channels_page(make, monkeypatch, number):
    env = make()
    saved = []
    monkeypatch.setattr("app.transfer_directory.reject_loops", lambda w, d: None)
    monkeypatch.setattr("app.transfer_directory.rpc", lambda name, payload: saved.append(payload) or True)
    response = client.post(URL, json={"goals": ["transfer"], "transfer_number": number})
    assert response.status_code == 422 and saved == []


def test_invalid_input_is_rejected(make):
    make()
    assert client.post(URL, json={"goals": []}).status_code == 422
    assert client.post(URL, json={"goals": ["hack"]}).status_code == 422
    assert client.post(URL, json={"goals": ["info"], "transfer_number": "555"}).status_code == 422
    assert client.post(URL, json={"goals": ["info"], "other": "x" * 201}).status_code == 422


def test_viewers_cannot_build(make, monkeypatch):
    env = make()
    def deny(*a, **k):
        raise HTTPException(404, "Workspace not found")
    monkeypatch.setattr(mod, "membership", deny)
    assert client.post(URL, json={"goals": ["info"]}).status_code == 404 and env.saved == []


# ---- phone numbers, understanding the business, and saving confirmed knowledge ----
@pytest.mark.parametrize("raw,expected", [
    ("9144460465", "+19144460465"), ("(914) 446-0465", "+19144460465"), ("1 914 446 0465", "+19144460465"),
    ("+1 914 446 0465", "+19144460465"), ("+44 20 7946 0958", "+442079460958")])
def test_phone_numbers_are_forgiving(raw, expected):
    assert mod.normalize_phone(raw) == expected


@pytest.mark.parametrize("raw", ["555", "0144460465", "+0123", "abcdefghij", "1144460465"])
def test_bad_phone_numbers_are_rejected(raw):
    with pytest.raises(ValueError):
        mod.normalize_phone(raw)


def test_a_plain_us_number_is_accepted_by_the_build_endpoint(make, monkeypatch):
    env = make()
    saved = []
    monkeypatch.setattr("app.transfer_directory.reject_loops", lambda w, d: None)
    monkeypatch.setattr("app.transfer_directory.rpc", lambda name, payload: saved.append(payload) or True)
    response = client.post(URL, json={"goals": ["transfer"], "transfer_number": "9144460465"})
    assert response.status_code == 200 and saved[0]["p_directory"][0]["number"] == "+19144460465"


def test_invented_prices_are_dropped_and_real_ones_kept():
    source = "Business cards start at $24.99 for 100. Flyers are $45 per 500."
    kept = mod.verified_prices([
        {"item": "Business cards", "price_text": "$24.99"}, {"item": "Flyers", "price_text": "$45"},
        {"item": "Banners", "price_text": "$99"}, {"item": "Posters", "price_text": "cheap"},
        {"item": "Business cards", "price_text": "$24.99"}], source)
    assert [(p["item"], p["price_text"]) for p in kept] == [("Business cards", "$24.99"), ("Flyers", "$45")]


def test_overview_never_contains_money_amounts():
    text = mod.overview_text({"summary": "We print cards.", "offerings": [
        {"name": "Cards", "description": "From $24.99"}, {"name": "Signs", "description": "Indoor and outdoor"}],
        "hours": "Mon-Fri 9-5", "phone": "", "email": "", "address": ""})
    assert "$" not in text and "Signs: Indoor and outdoor" in text and "Hours: Mon-Fri 9-5" in text


def test_deterministic_fallback_finds_prices_and_a_summary():
    data = mod.deterministic_understanding("Metro Print makes cards.\nBusiness cards from $24.99\nOpen daily")
    assert data["prices"][0]["price_text"] == "$24.99" and data["summary"].startswith("Metro Print")


def test_analyze_needs_exactly_one_source_and_enough_text(make, monkeypatch):
    make()
    monkeypatch.setattr(mod, "llm_understanding", lambda text, kind=None, subtype=None: None)
    assert client.post(URL + "/analyze", json={}).status_code == 422
    assert client.post(URL + "/analyze", json={"text": "short"}).status_code == 422
    assert client.post(URL + "/analyze", json={"text": "x" * 30, "document_id": "00000000-0000-0000-0000-000000000001"}).status_code == 422


def test_analyze_uses_the_model_but_filters_what_it_invents(make, monkeypatch):
    make()
    model = {"summary": "Print shop", "offerings": [{"name": "Cards", "description": ""}], "hours": "", "phone": "", "email": "",
             "address": "", "prices": [{"item": "Cards", "price_text": "$24.99"}, {"item": "Ghost", "price_text": "$5"}]}
    monkeypatch.setattr(mod, "llm_understanding", lambda text, kind=None, subtype=None: dict(model))
    monkeypatch.setattr(mod, "_analyze_hits", mod.deque())
    result = client.post(URL + "/analyze", json={"text": "We print cards. Business cards cost $24.99 each."}).json()
    assert [p["item"] for p in result["understanding"]["prices"]] == ["Cards"] and "Ghost" not in str(result)


def test_analyze_falls_back_when_the_model_is_unavailable(make, monkeypatch):
    make()
    monkeypatch.setattr(mod, "llm_understanding", lambda text, kind=None, subtype=None: None)
    monkeypatch.setattr(mod, "_analyze_hits", mod.deque())
    result = client.post(URL + "/analyze", json={"text": "We print cards.\nCards from $24.99 each\nOpen daily"}).json()
    assert result["understanding"]["prices"][0]["price_text"] == "$24.99"


class Knowledge:
    def __init__(self):
        self.calls = []

    def install(self, monkeypatch):
        monkeypatch.setattr("app.account.create_description", lambda w, b, u: self.calls.append(("doc", b.content)) or {"id": "d1"})
        monkeypatch.setattr("app.account.approve_description", lambda w, d, u: self.calls.append(("approve_doc", d)))
        monkeypatch.setattr("app.account.set_verified_pricing", lambda w, b, u: self.calls.append(("pricing", b.enabled)))
        monkeypatch.setattr("app.account.create_fact", lambda w, b, u: self.calls.append(("fact", b.question, b.answer)) or {"id": "f%d" % len(self.calls)})
        monkeypatch.setattr("app.account.approve_fact", lambda w, f, u: self.calls.append(("approve_fact", f)))


def test_confirmed_prices_enable_pricing_and_become_approved_facts(make, monkeypatch):
    make()
    k = Knowledge(); k.install(monkeypatch)
    body = {"overview": "We print business cards and signs for local shops.", "prices_confirmed": True,
            "prices": [{"item": "Business cards", "price_text": "$24.99"}]}
    result = client.post(URL + "/knowledge", json=body).json()
    assert result == {"overview_saved": True, "prices_saved": 1, "prices_enabled": True, "products_saved": 0, "policies_saved": 0}
    assert ("pricing", True) in k.calls and ("fact", "How much is Business cards?", "Business cards: $24.99") in k.calls


def test_unconfirmed_prices_are_not_published(make, monkeypatch):
    make()
    k = Knowledge(); k.install(monkeypatch)
    body = {"overview": "We print business cards and signs for local shops.", "prices_confirmed": False,
            "prices": [{"item": "Business cards", "price_text": "$24.99"}]}
    result = client.post(URL + "/knowledge", json=body).json()
    assert result["prices_saved"] == 0 and not any(c[0] in ("pricing", "fact") for c in k.calls)


def test_overview_with_money_is_refused(make, monkeypatch):
    make()
    k = Knowledge(); k.install(monkeypatch)
    response = client.post(URL + "/knowledge", json={"overview": "Business cards cost $24.99 and we are open daily."})
    assert response.status_code == 422 and k.calls == []


# ---- business kinds: products vs services, and clinic safety ----
def test_clinic_instructions_are_safe_and_minimal():
    _, text = mod.compose("Bright Smile Clinic", "Health", "Maya", ["info", "booking", "messages"], None, "services", "clinic", booking_live=True)
    for phrase in ("Never give medical advice", "call 911", "new or returning patient", "Do not ask for symptoms", "insurance"):
        assert phrase in text
    assert "usual length" in text


def test_booking_without_a_calendar_collects_a_request_instead_of_claiming_to_book():
    _, text = mod.compose("Bright Smile Clinic", None, "Maya", ["booking"], None, "services", "clinic", booking_live=False)
    assert "cannot book directly yet" in text and "check availability first" not in text


def test_products_and_services_get_different_guidance():
    _, products = mod.compose("Metro Print", None, "Alex", ["info", "orders"], None, "products", "custom")
    _, services = mod.compose("Metro Print", None, "Alex", ["info", "booking"], None, "services", "trades", booking_live=True)
    assert "sells products" in products and "provides services" not in products
    assert "provides services" in services and "sells products" not in services


def test_no_kind_keeps_the_generic_instructions():
    _, text = mod.compose("Metro Print", None, "Alex", ["info"], None)
    assert "sells products" not in text and "provides services" not in text and "clinic" not in text.lower()


def test_kind_and_type_are_validated_and_saved(make):
    env = make(calendar=True)
    ok = client.post(URL, json={"kind": "services", "subtype": "clinic", "goals": ["booking"]})
    assert ok.status_code == 200 and env.patched[0]["business_kind"] == "services" and env.patched[0]["business_type"] == "clinic"
    assert client.post(URL, json={"kind": "services", "subtype": "custom", "goals": ["info"]}).status_code == 422
    assert client.post(URL, json={"kind": "pirates", "goals": ["info"]}).status_code == 422


def test_booking_goal_without_calendar_still_saves_requests(make):
    env = make(calendar=False)
    result = client.post(URL, json={"kind": "services", "subtype": "clinic", "goals": ["booking"]}).json()
    assert env.patched[0]["voice_leads_enabled"] is True and env.patched[0]["voice_booking_enabled"] is False
    assert "cannot book directly yet" in result["instructions"] and any(n["key"] == "calendar" for n in result["needs"])


def test_status_offers_kinds_and_presets(make, monkeypatch):
    make()
    body = client.get(URL).json()
    assert set(body["kinds"]) == {"products", "services"} and "clinic" in body["kinds"]["services"]["types"]
    assert body["presets"]["services:clinic"] == ["info", "booking", "messages", "transfer"]


def test_services_and_policies_reach_the_overview_without_prices():
    text = mod.overview_text({"summary": "Family clinic.", "offerings": [], "services": [
        {"name": "Checkup", "description": "Routine visit", "duration": "30 min"}, {"name": "Cleaning", "description": "From $90", "duration": ""}],
        "policies": ["Cancel 24 hours ahead", "We accept most insurance"], "hours": "Mon-Fri 8-5", "phone": "", "email": "", "address": ""})
    assert "Service: Checkup: Routine visit (30 min)" in text and "Good to know: Cancel 24 hours ahead" in text and "$" not in text


# ---- whole-website understanding and publishing products ----
FAKE_SITE = {
    "source_url": "https://shop.example.com/", "title": "Shop", "description": "We print business cards, flyers and banners.",
    "pages": [], "text": {"home": "We print things.", "key": "Call us.", "policy": ""}, "email": "hi@shop.example.com",
    "phone": "(973) 555-0100", "hours": ["Mon-Fri 9-5"],
    "products": [
        {"name": "Flyers", "category": "Print Materials", "description": "Eye-catching flyers.", "options": [{"label": "Size", "values": ["4x6", "8.5x11"]}],
         "from_price": "$42.49", "details": "Flyers (Print Materials). Eye-catching flyers. Options — Size: 4x6, 8.5x11.",
         "price_note": "Starting at $42.49.", "url": "https://shop.example.com/products/flyers"},
        {"name": "Banners", "category": "Large Format", "description": "", "options": [], "from_price": "", "details": "Banners (Large Format).",
         "price_note": "", "url": "https://shop.example.com/products/banners"}],
    "policies": [{"title": "Refund Policy", "url": "https://shop.example.com/refund-policy", "text": "Refunds are issued before production starts. " * 3}],
    "stats": {"pages_read": 12, "products": 2, "seconds": 3.1},
}


def test_analyze_reads_the_whole_site_and_returns_exact_products(make, monkeypatch):
    make()
    monkeypatch.setattr(mod, "llm_understanding", lambda text, kind=None, subtype=None: None)
    monkeypatch.setattr(mod, "_analyze_hits", mod.deque())
    monkeypatch.setattr("app.site_reader.read_site", lambda url: FAKE_SITE)
    mod_api = mod.api
    monkeypatch.setattr(mod, "api", lambda table, method="GET", params=None, **kw: (
        [{"content": "old junk", "source_url": "https://shop.example.com/", "title": "Shop"}] if table == "knowledge_documents" else mod_api(table, method, params=params, **kw)))
    response = client.post(URL + "/analyze", json={"document_id": "00000000-0000-0000-0000-000000000001"})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["stats"]["pages_read"] == 12 and [p["name"] for p in result["products"]] == ["Flyers", "Banners"]
    assert result["products"][0]["from_price"] == "$42.49" and result["understanding"]["summary"].startswith("We print business cards")
    assert result["understanding"]["email"] == "hi@shop.example.com" and result["understanding"]["hours"] == "Mon-Fri 9-5"
    assert "old junk" not in str(result) and "$" not in result["overview"]
    assert [p["title"] for p in result["policy_pages"]] == ["Refund Policy"]


class Publish:
    def __init__(self):
        self.calls, self.entries = [], []

    def install(self, monkeypatch):
        monkeypatch.setattr("app.account.create_description", lambda w, b, u: {"id": "doc1"})
        monkeypatch.setattr("app.account.approve_description", lambda w, d, u: None)
        monkeypatch.setattr("app.account.set_verified_pricing", lambda w, b, u: self.calls.append(("pricing", b.enabled)))
        monkeypatch.setattr(mod, "publish_facts", lambda w, doc, entries, uid: self.entries.extend(entries) or len(entries))


PRODUCTS = [{"name": "Flyers", "details": "Flyers. Eye-catching. Options — Size: 4x6. Starts at $5 in bulk.", "price_note": "Starting at $42.49."}]
POLICIES = [{"title": "Refund Policy", "text": "Refunds are issued before production starts. Late fee is $10. Contact us."}]


def test_products_are_published_without_prices_until_the_owner_confirms(make, monkeypatch):
    make()
    pub = Publish(); pub.install(monkeypatch)
    result = client.post(URL + "/knowledge", json={"overview": "We print business cards and flyers for shops.", "products": PRODUCTS,
                                                    "policies": POLICIES, "prices_confirmed": False}).json()
    assert result["products_saved"] == 1 and result["policies_saved"] == 1 and result["prices_enabled"] is False
    assert ("pricing", True) not in pub.calls
    for question, answer, kind in pub.entries:
        assert "$" not in answer and "prices" not in question
    assert any("Eye-catching" in a for _, a, _ in pub.entries)


def test_confirmed_prices_enable_pricing_and_are_added_to_each_product_answer(make, monkeypatch):
    make()
    pub = Publish(); pub.install(monkeypatch)
    result = client.post(URL + "/knowledge", json={"overview": "We print business cards and flyers for shops.", "products": PRODUCTS,
                                                    "prices_confirmed": True}).json()
    assert ("pricing", True) in pub.calls and result["prices_enabled"] is True and result["products_saved"] == 1
    question, answer, _ = pub.entries[0]
    assert "Starting at $42.49." in answer and "prices" in question and "Starts at $5" not in answer


def test_without_money_drops_only_the_sentences_with_amounts():
    assert mod.without_money("Flyers are great. They start at $5. Ask about sizes.") == "Flyers are great. Ask about sizes."


def test_every_analysis_attempt_is_counted_before_any_download(make, monkeypatch):
    make()
    reads = []
    monkeypatch.setattr(mod, "_analyze_hits", mod.deque())
    monkeypatch.setattr("app.site_reader.read_site", lambda url: reads.append(url) or FAKE_SITE)
    monkeypatch.setattr(mod, "allow_analyze", lambda workspace: False)
    inner = mod.api
    monkeypatch.setattr(mod, "api", lambda table, method="GET", params=None, **kw: (
        [{"content": "x" * 50, "source_url": "https://shop.example.com/", "title": "Shop"}] if table == "knowledge_documents" else inner(table, method, params=params, **kw)))
    response = client.post(URL + "/analyze", json={"document_id": "00000000-0000-0000-0000-000000000001"})
    assert response.status_code == 429 and reads == []


def test_only_a_few_crawls_run_at_once(make, monkeypatch):
    make()
    reads = []
    monkeypatch.setattr(mod, "_analyze_hits", mod.deque())
    monkeypatch.setattr("app.site_reader.read_site", lambda url: reads.append(url) or FAKE_SITE)
    monkeypatch.setattr(mod, "llm_understanding", lambda text, kind=None, subtype=None: None)
    slots = mod.BoundedSemaphore(1)
    slots.acquire()
    monkeypatch.setattr(mod, "CRAWL_SLOTS", slots)
    inner = mod.api
    monkeypatch.setattr(mod, "api", lambda table, method="GET", params=None, **kw: (
        [{"content": "x" * 50, "source_url": "https://shop.example.com/", "title": "Shop"}] if table == "knowledge_documents" else inner(table, method, params=params, **kw)))
    response = client.post(URL + "/analyze", json={"document_id": "00000000-0000-0000-0000-000000000001"})
    assert response.status_code == 429 and reads == []
    slots.release()
    assert client.post(URL + "/analyze", json={"document_id": "00000000-0000-0000-0000-000000000001"}).status_code == 200


def test_product_names_become_speech_keywords_without_losing_the_owners_terms(monkeypatch):
    store = {"speech_keyterms": ["Metro", "metroprint"]}
    patches = []

    def api(table, method="GET", params=None, data=None, **kw):
        if method == "GET":
            return [dict(store)]
        patches.append(data)
        return []

    monkeypatch.setattr(mod, "api", api)
    mod.add_keyterms("w", ["Business Cards", "metro", "Flyers", "x", "Flyers"] + ["Product %d" % i for i in range(40)])
    terms = patches[0]["speech_keyterms"]
    assert terms[:2] == ["Metro", "metroprint"] and "Business Cards" in terms and "Flyers" in terms
    assert len(terms) == 25 and len({t.casefold() for t in terms}) == 25 and "x" not in terms


def test_keyword_trouble_never_blocks_saving_knowledge(monkeypatch):
    def api(*args, **kwargs):
        raise RuntimeError("database down")
    monkeypatch.setattr(mod, "api", api)
    mod.add_keyterms("w", ["Flyers"])  # must not raise


def test_the_agent_prompt_teaches_a_smooth_callback_flow(monkeypatch):
    from app import cloud
    monkeypatch.setattr("app.voice_actions.feature_flags", lambda oid: {"voice_leads_enabled": True, "call_instructions": ""})
    text = cloud.receptionist_prompt("00000000-0000-0000-0000-00000000000a", "Metro Print", "Alex")
    for phrase in ("never repeat it", "Is the number you're calling from the best one", "do not ask for digits",
                   "ten digits", "has already agreed to a callback", "sounds annoyed", "Sound warm and human", "Offer a team quote",
                   "do not ask them to describe it first", "very briefly what you offer", "don't have their number from this call"):
        assert phrase in text
    assert "repeat them back" not in text


def test_category_overviews_list_the_options_briefly_and_only_show_prices_when_confirmed():
    items = [mod.ProductChoice(name="Standard Business Cards", details="x" * 10, category="Business Cards", from_price="$20"),
             mod.ProductChoice(name="Premium Business Cards", details="x" * 10, category="Business Cards", from_price="$25.49"),
             mod.ProductChoice(name="Flyers", details="x" * 10, category="Print Materials", from_price="$42.49"),
             mod.ProductChoice(name="Loose item", details="x" * 10)]
    with_prices = mod.category_overviews(items, True)
    questions = [q for q, _, _ in with_prices]
    assert questions[0].startswith("What Business Cards do you offer?") and questions[-1].startswith("What do you sell?")
    assert "2 Business Cards: Standard Business Cards from $20, Premium Business Cards from $25.49." in with_prices[0][1]
    assert "Business Cards (2), Print Materials (1)" in with_prices[-1][1] and all(kind == "category" for _, _, kind in with_prices)
    without = mod.category_overviews(items, False)
    assert all("$" not in answer for _, answer, _ in without) and "Standard Business Cards, Premium Business Cards" in without[0][1]
    assert mod.category_overviews([mod.ProductChoice(name="Solo", details="x" * 10)], True) == []


def test_saving_publishes_category_overviews_too(make, monkeypatch):
    make()
    pub = Publish(); pub.install(monkeypatch)
    products = [{"name": "Flyers", "details": "Flyers. Great.", "price_note": "Starting at $42.49.", "category": "Print Materials", "from_price": "$42.49"},
                {"name": "Menus", "details": "Menus. Great.", "price_note": "", "category": "Print Materials", "from_price": ""}]
    result = client.post(URL + "/knowledge", json={"overview": "We print business cards and flyers for shops.", "products": products,
                                                    "prices_confirmed": True}).json()
    kinds = [kind for _, _, kind in pub.entries]
    assert kinds.count("product") == 2 and kinds.count("category") == 2 and result["products_saved"] == 2
    assert any("Flyers from $42.49" in answer for _, answer, kind in pub.entries if kind == "category")
