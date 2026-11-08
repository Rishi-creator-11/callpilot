"""LobbyStack connection: signatures, retry dedupe, tenant matching, idempotent provisioning."""
import base64, hashlib, hmac, json, time
import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import lobbystack as mod
from app.account import current_user
from app.main import app

client = TestClient(app)
W1, W2 = "00000000-0000-0000-0000-0000000000a1", "00000000-0000-0000-0000-0000000000a2"
SECRET = "whsec_" + base64.b64encode(b"k" * 24).decode()


def sign(raw, msg_id="evt1", stamp=None, secret=SECRET):
    stamp = str(int(stamp or time.time()))
    mac = hmac.new(base64.b64decode(secret[6:]), msg_id.encode() + b"." + stamp.encode() + b"." + raw, hashlib.sha256).digest()
    return {"webhook-id": msg_id, "webhook-timestamp": stamp, "webhook-signature": "v1," + base64.b64encode(mac).decode()}


class Db:
    def __init__(self, key):
        self.events, self.businesses, self.patched = {}, {}, []
        self.key = key
        enc = Fernet(key).encrypt(SECRET.encode()).decode()
        for oid, biz in ((W1, "biz1"), (W2, "biz2")):
            self.businesses[biz] = {"organization_id": oid, "business_id": biz, "webhook_secret_ciphertext": enc,
                                    "webhook_verified_at": None, "calendar_status": "not_connected"}

    def api(self, table, method="GET", params=None, data=None, prefer=None):
        params = params or {}
        if table == "lobbystack_businesses":
            if method == "PATCH":
                self.patched.append(data); return []
            if method == "POST":
                self.businesses[data["business_id"]] = {**data, "webhook_verified_at": None, "calendar_status": "not_connected"}; return []
            if "business_id" in params:
                return [self.businesses[params["business_id"][3:]]] if params["business_id"][3:] in self.businesses else []
            oid = params["organization_id"][3:]
            return [b for b in self.businesses.values() if b["organization_id"] == oid]
        if table == "lobbystack_events":
            if method == "POST":
                if data["event_id"] in self.events: return []
                self.events[data["event_id"]] = data; return [data]
            return [e for e in self.events.values() if e["organization_id"] == params["organization_id"][3:]]
        return []


@pytest.fixture
def db(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("GOOGLE_TOKEN_ENCRYPTION_KEY", key)
    d = Db(key.encode())
    monkeypatch.setattr(mod, "api", d.api)
    return d


def post(event, headers):
    return client.post("/webhooks/lobbystack", content=json.dumps(event).encode(), headers=headers)


def event(biz="biz1", eid="evt1", kind="call.completed"):
    return {"id": eid, "type": kind, "business_id": biz, "created_at": "2026-11-08T10:00:00Z", "data": {"id": "call9", "outcome": "appointment_booked"}}


def test_valid_signature_stores_once_and_retry_is_duplicate(db):
    ev = event(); raw = json.dumps(ev).encode()
    assert client.post("/webhooks/lobbystack", content=raw, headers=sign(raw)).json()["duplicate"] is False
    assert client.post("/webhooks/lobbystack", content=raw, headers=sign(raw)).json()["duplicate"] is True
    assert len(db.events) == 1 and db.events["evt1"]["organization_id"] == W1
    assert db.patched and "webhook_verified_at" in db.patched[0]


def test_bad_stale_and_wrong_tenant_signatures_are_rejected(db):
    raw = json.dumps(event()).encode()
    assert client.post("/webhooks/lobbystack", content=raw, headers={**sign(raw), "webhook-signature": "v1,AAAA"}).status_code == 401
    assert client.post("/webhooks/lobbystack", content=raw, headers=sign(raw, stamp=time.time() - 3600)).status_code == 401
    other = "whsec_" + base64.b64encode(b"z" * 24).decode()
    assert client.post("/webhooks/lobbystack", content=raw, headers=sign(raw, secret=other)).status_code == 401
    raw2 = json.dumps(event(biz="nobody")).encode()
    assert client.post("/webhooks/lobbystack", content=raw2, headers=sign(raw2)).status_code == 401
    assert db.events == {}


def test_event_lands_in_the_workspace_of_its_business(db):
    raw = json.dumps(event(biz="biz2", eid="e2")).encode()
    assert client.post("/webhooks/lobbystack", content=raw, headers=sign(raw, "e2")).status_code == 200
    assert db.events["e2"]["organization_id"] == W2


def test_body_tampering_fails_signature(db):
    raw = json.dumps(event()).encode(); headers = sign(raw)
    assert client.post("/webhooks/lobbystack", content=raw.replace(b"call9", b"call8"), headers=headers).status_code == 401


def provision_with(db, monkeypatch, role="owner", email="Owner@Example.com"):
    calls = []
    def fake(method, path, **kw):
        calls.append((method, path, kw.get("idem"), kw.get("business"), kw.get("body")))
        return {"data": {"id": "biz9", "secret": SECRET}}
    monkeypatch.setattr(mod, "call", fake)
    monkeypatch.setattr(mod, "org_or_404", lambda o: {"name": "Barbers", "timezone": "America/New_York"})
    app.dependency_overrides[current_user] = lambda: {"id": "u1", "email": email}
    monkeypatch.setattr(mod, "membership", lambda *a, **k: role)
    return calls


def test_provision_invites_the_owner_to_lobbystack_and_presets_nothing(db, monkeypatch):
    calls = provision_with(db, monkeypatch)
    wid = "00000000-0000-0000-0000-0000000000a3"
    try:
        r = client.post("/api/account/organizations/%s/lobbystack/provision" % wid, json={})
        assert r.status_code == 200 and r.json()["connected"] is True
        method, path, idem, biz, body = calls[0]
        assert (method, path, idem, biz) == ("POST", "/businesses", "callpilot-v2-" + wid, None)
        assert body["owner_email"] == "owner@example.com" and body["access_level"] == "full"
        # LobbyStack's own defaults stay in force: no style, greeting or prompt is pushed to the receptionist.
        assert not any(path.startswith("/receptionist") or path.startswith("/knowledge") for _, path, *_ in calls)
        assert calls[1][1] == "/webhooks" and calls[1][3] == "biz9"
        saved = db.businesses["biz9"]
        assert saved["owner_invited_email"] == "owner@example.com"
        def deny(*a, **k): raise HTTPException(404, "Workspace not found")
        monkeypatch.setattr(mod, "membership", deny)
        assert client.post("/api/account/organizations/%s/lobbystack/provision" % W1, json={}).status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_only_the_workspace_owner_is_invited_never_an_admin(db, monkeypatch):
    calls = provision_with(db, monkeypatch, role="admin", email="admin@example.com")
    try:
        r = client.post("/api/account/organizations/%s/lobbystack/provision" % "00000000-0000-0000-0000-0000000000a4", json={})
        assert r.status_code == 200
        assert "owner_email" not in calls[0][4]
        assert "owner_invited_email" not in db.businesses["biz9"]
    finally:
        app.dependency_overrides.clear()


def test_dashboard_endpoint_returns_the_partner_dashboard_address(db, monkeypatch):
    monkeypatch.delenv("LOBBYSTACK_DASHBOARD_URL", raising=False)
    mod._dashboard_cache.update(at=0.0, url="")
    db.businesses["biz1"]["owner_invited_email"] = "owner@example.com"
    monkeypatch.setattr(mod, "call", lambda method, path, **kw: {"data": {"dashboard_url": "https://app.callpilot.example"}})
    app.dependency_overrides[current_user] = lambda: {"id": "u1", "email": "owner@example.com"}
    monkeypatch.setattr(mod, "membership", lambda *a, **k: "owner")
    try:
        r = client.get("/api/account/organizations/%s/lobbystack/dashboard" % W1)
        assert r.status_code == 200
        body = r.json()
        assert body["dashboard_url"] == "https://app.callpilot.example" and body["owner_invited"] is True
        assert set(body) == {"dashboard_url", "business_id", "owner_invited_email", "owner_invited"}  # no secrets
    finally:
        app.dependency_overrides.clear()
        mod._dashboard_cache.update(at=0.0, url="")


def test_dashboard_address_falls_back_to_lobbystack_and_rejects_non_https(monkeypatch):
    mod._dashboard_cache.update(at=0.0, url="")
    monkeypatch.delenv("LOBBYSTACK_DASHBOARD_URL", raising=False)
    monkeypatch.setattr(mod, "call", lambda *a, **k: {"data": {"dashboard_url": "http://evil.example"}})
    assert mod.dashboard_url() == "https://app.lobbystack.com"
    def down(*a, **k): raise HTTPException(503, "down")
    monkeypatch.setattr(mod, "call", down)
    assert mod.dashboard_url() == "https://app.lobbystack.com"


def test_receptionist_screen_hands_off_to_the_hosted_dashboard():
    page = client.get("/app").text
    assert 'id="rcHosted"' in page and "Open your receptionist" in page
    assert "#rcMain.rc-hosted>article:not(#rcHosted):not(#rcCallsCard){display:none}" in page


def test_missing_platform_key_is_a_clear_503(monkeypatch):
    monkeypatch.delenv("LOBBYSTACK_PLATFORM_KEY", raising=False)
    with pytest.raises(HTTPException) as e:
        mod.call("GET", "/me")
    assert e.value.status_code == 503


def test_apply_profile_removes_duplicates_updates_prices_and_never_duplicates(monkeypatch):
    log = []
    knowledge = [{"id": "a", "title": "How much is a haircut and how long does it take?"},
                 {"id": "b", "title": "How much is a haircut and how long does it take?"}]

    def fake(method, path, **kw):
        log.append((method, path))
        if method == "GET" and path == "/knowledge":
            return {"data": list(knowledge), "next_cursor": None, "has_more": False}
        if method == "GET" and path == "/services":
            return {"data": [{"id": "s1", "name": "Haircut", "description": "old", "duration_minutes": 20}]}
        if path.startswith("/services/"):
            return {"data": {"id": "s1", "name": "Haircut", "duration_minutes": 20}}
        return {"data": {}}

    monkeypatch.setattr(mod, "call", fake)
    monkeypatch.setattr(mod, "require_link", lambda o: {"business_id": "biz"})
    monkeypatch.setattr(mod, "membership", lambda *a, **k: "owner")
    body = mod.Profile(services=[mod.ServiceIn(name="Haircut", duration_minutes=20, price_text="$40")], booking_mode="instant")
    result = mod.apply_profile(W1, body, {"id": "u"})
    assert result["duplicates_removed"] == 1 and result["duplicates_left"] == 0
    assert ("DELETE", "/knowledge/b") in log and ("DELETE", "/knowledge/a") not in log
    assert ("PATCH", "/services/s1") in log
    assert ("POST", "/knowledge") in log  # only the price list is new; the haircut FAQ already exists
    assert sum(1 for m_, p_ in log if (m_, p_) == ("POST", "/knowledge")) == 1


def test_listing_follows_pages(monkeypatch):
    pages = {None: {"data": [1, 2], "next_cursor": "c2", "has_more": True}, "c2": {"data": [3], "next_cursor": None, "has_more": False}}
    monkeypatch.setattr(mod, "call", lambda method, path, **kw: pages[(kw.get("params") or {}).get("cursor")])
    assert mod.listing("/knowledge", "biz") == [1, 2, 3]


MENU = """Dinner Menu
Appetizers & Small Plates
Duck Croquettes
Duck Confit, Green Garlic
21
Jumbo Lump Crab Cake
Harissa Aioli & Preserved Citrus
27
Daily Crudo MP
Satis Signatures
Truffle-Roasted Organic Half Chicken
Fava Beans, Morels
35
Closing events text that has no price
"""


def test_menu_parser_reads_sections_dishes_and_market_price():
    from app.menu import parse, answers
    sections = parse(MENU)
    assert [s["name"] for s in sections] == ["Dinner Menu Appetizers & Small Plates", "Satis Signatures"] or [s["name"] for s in sections][-1] == "Satis Signatures"
    items = {i["name"]: i["price"] for s in sections for i in s["items"]}
    assert items["Jumbo Lump Crab Cake"] == "$27" and items["Duck Croquettes"] == "$21"
    assert items["Daily Crudo"] == "market price" and items["Truffle-Roasted Organic Half Chicken"] == "$35"
    faq = dict(answers("Dinner menu", sections))
    assert faq["How much is the jumbo lump crab cake on the dinner menu?"].startswith("On the dinner menu, the Jumbo Lump Crab Cake is $27.")
    assert "ask the team" in faq["How much is the daily crudo on the dinner menu?"]


def test_menu_import_is_idempotent_and_rejects_text_without_prices(monkeypatch):
    saved = {}
    def fake(method, path, **kw):
        if method == "GET" and path == "/knowledge":
            return {"data": [{"id": k, "title": k, "content": v} for k, v in saved.items()], "next_cursor": None, "has_more": False}
        if method == "POST":
            saved[kw["body"]["question"]] = kw["body"]["answer"]
        return {"data": {}}
    monkeypatch.setattr(mod, "call", fake)
    monkeypatch.setattr(mod, "require_link", lambda o: {"business_id": "biz"})
    monkeypatch.setattr(mod, "membership", lambda *a, **k: "owner")
    first = mod.import_menu(W1, mod.Menu(title="Dinner menu", text=MENU), {"id": "u"})
    again = mod.import_menu(W1, mod.Menu(title="Dinner menu", text=MENU), {"id": "u"})
    assert first["added"] > 0 and again["added"] == 0 and again["updated"] == 0
    with pytest.raises(HTTPException) as e:
        mod.import_menu(W1, mod.Menu(title="Dinner menu", text="We serve food. Come visit us any day of the week."), {"id": "u"})
    assert e.value.status_code == 422


def test_same_dish_on_two_menus_keeps_both_prices(monkeypatch):
    from app.menu import parse, answers
    dinner = dict(answers("Dinner menu", parse("Salads\nTricolore Salad\nRadicchio\n18\n")))
    lunch = dict(answers("Lunch menu", parse("Salads\nTricolore Salad\nRadicchio\n16\n")))
    assert dinner["How much is the tricolore salad on the dinner menu?"].endswith("$18. It comes with Radicchio.")
    assert "$16" in lunch["How much is the tricolore salad on the lunch menu?"]
    assert not set(dinner) & set(lunch)


def test_section_heading_without_ingredients_is_not_a_dish():
    from app.menu import parse
    sections = parse("Entrees\nSalmon\nLentils, Dill\n38\nSides\nBraised Red Cabbage\n12\nRoasted Carrots\n12\n")
    names = {s["name"]: [i["name"] for i in s["items"]] for s in sections}
    assert names["Sides"] == ["Braised Red Cabbage", "Roasted Carrots"]
    assert all(i["name"] != "Sides" for s in sections for i in s["items"])


def test_decimal_prices_keep_two_places():
    from app.menu import parse
    items = [i for s in parse("Beverages\nAmerican Coffee\n2.5\nLatte\n4.50\n") for i in s["items"]]
    assert [i["price"] for i in items] == ["$2.50", "$4.50"]


def test_cleanup_clears_only_the_exact_injected_prompt(db, monkeypatch):
    monkeypatch.setenv("LOBBYSTACK_PLATFORM_KEY", "k")
    db.businesses["biz1"]["business_id"] = "biz1"
    patched, current = [], {"biz1": mod.HUMAN_STYLE, "biz2": "An owner wrote this themselves."}
    def fake(method, path, **kw):
        if method == "GET": return {"data": {"voice_instructions": current[kw["business"]]}}
        patched.append((kw["business"], kw["body"])); return {"data": {}}
    monkeypatch.setattr(mod, "call", fake)
    monkeypatch.setattr(mod, "api", lambda table, **kw: [{"business_id": "biz1"}, {"business_id": "biz2"}])
    assert mod.clear_injected_style() == 1
    assert patched == [("biz1", {"voice_instructions": None})]
    assert mod.clear_injected_style() == 1  # a second run finds nothing new only once LobbyStack reports it cleared
    current["biz1"] = None
    patched.clear()
    assert mod.clear_injected_style() == 0 and patched == []


def test_cleanup_does_nothing_without_a_platform_key(monkeypatch):
    monkeypatch.delenv("LOBBYSTACK_PLATFORM_KEY", raising=False)
    assert mod.clear_injected_style() == 0
