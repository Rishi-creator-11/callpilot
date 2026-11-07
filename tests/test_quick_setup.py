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
    monkeypatch.setattr(mod, "llm_understanding", lambda text: None)
    assert client.post(URL + "/analyze", json={}).status_code == 422
    assert client.post(URL + "/analyze", json={"text": "short"}).status_code == 422
    assert client.post(URL + "/analyze", json={"text": "x" * 30, "document_id": "00000000-0000-0000-0000-000000000001"}).status_code == 422


def test_analyze_uses_the_model_but_filters_what_it_invents(make, monkeypatch):
    make()
    model = {"summary": "Print shop", "offerings": [{"name": "Cards", "description": ""}], "hours": "", "phone": "", "email": "",
             "address": "", "prices": [{"item": "Cards", "price_text": "$24.99"}, {"item": "Ghost", "price_text": "$5"}]}
    monkeypatch.setattr(mod, "llm_understanding", lambda text: dict(model))
    monkeypatch.setattr(mod, "_analyze_hits", mod.deque())
    result = client.post(URL + "/analyze", json={"text": "We print cards. Business cards cost $24.99 each."}).json()
    assert [p["item"] for p in result["understanding"]["prices"]] == ["Cards"] and "Ghost" not in str(result)


def test_analyze_falls_back_when_the_model_is_unavailable(make, monkeypatch):
    make()
    monkeypatch.setattr(mod, "llm_understanding", lambda text: None)
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
    assert result == {"overview_saved": True, "prices_saved": 1, "prices_enabled": True}
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
