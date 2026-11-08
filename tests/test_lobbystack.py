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


def test_provision_uses_stable_idempotency_key_and_requires_owner(db, monkeypatch):
    calls = []
    def fake(method, path, **kw):
        calls.append((method, path, kw.get("idem"), kw.get("business")))
        return {"data": {"id": "biz9", "secret": SECRET}}
    monkeypatch.setattr(mod, "call", fake)
    monkeypatch.setattr(mod, "org_or_404", lambda o: {"name": "Barbers", "timezone": "America/New_York"})
    app.dependency_overrides[current_user] = lambda: {"id": "u1"}
    monkeypatch.setattr(mod, "membership", lambda *a, **k: "owner")
    try:
        r = client.post("/api/account/organizations/%s/lobbystack/provision" % "00000000-0000-0000-0000-0000000000a3", json={})
        assert r.status_code == 200 and r.json()["connected"] is True
        assert calls[0] == ("POST", "/businesses", "callpilot-00000000-0000-0000-0000-0000000000a3", None)
        assert calls[1][:2] == ("PATCH", "/receptionist") and calls[1][3] == "biz9"
        assert calls[2][1] == "/webhooks" and calls[2][3] == "biz9"
        def deny(*a, **k): raise HTTPException(404, "Workspace not found")
        monkeypatch.setattr(mod, "membership", deny)
        assert client.post("/api/account/organizations/%s/lobbystack/provision" % W1, json={}).status_code == 404
    finally:
        app.dependency_overrides.clear()


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
    assert faq["How much is the jumbo lump crab cake?"].startswith("The Jumbo Lump Crab Cake is $27.")
    assert "ask the team" in faq["How much is the daily crudo?"]


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
