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
        assert calls[1][1] == "/webhooks" and calls[1][3] == "biz9"
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
