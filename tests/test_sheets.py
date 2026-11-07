"""Google Sheets lead export: least-privilege connection, idempotent rows, failure isolation."""
import hashlib

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import sheets
from app.account import current_user
from app.main import app

client = TestClient(app)
WORKSPACE = "00000000-0000-0000-0000-00000000000a"
URL = "/api/account/organizations/%s/sheets" % WORKSPACE
SAVED = {"organization_id": WORKSPACE, "refresh_token_ciphertext": "x", "spreadsheet_id": "sheet1",
         "spreadsheet_url": "https://docs.google.com/spreadsheets/d/sheet1", "connected_at": "2026-11-07T00:00:00Z"}


class Store:
    def __init__(self, saved=SAVED):
        self.saved, self.exports, self.states, self.rows = saved, set(), [], []
        self.fail_append = False

    def api(self, table, method="GET", params=None, data=None, prefer=None, **kw):
        if table == "sheet_connections":
            if method == "GET":
                return [dict(self.saved)] if self.saved else []
            if method == "DELETE":
                self.saved = None
            return []
        if table == "sheet_exports":
            call = (data or {}).get("provider_call_id") or params["provider_call_id"][3:]
            if method == "POST":
                if call in self.exports:
                    return []
                self.exports.add(call)
                return [data]
            self.exports.discard(call)
            return []
        if table == "oauth_states":
            self.states.append(data)
            return []
        return []

    def append(self, saved, values):
        if self.fail_append:
            raise HTTPException(502, "boom")
        self.rows.append(values)


@pytest.fixture
def store(monkeypatch):
    state = Store()
    monkeypatch.setattr(sheets, "api", state.api)
    monkeypatch.setattr(sheets, "append_row", state.append)
    monkeypatch.setattr(sheets, "membership", lambda *a, **k: "owner")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "secret")
    monkeypatch.setenv("GOOGLE_TOKEN_ENCRYPTION_KEY", "A" * 43 + "=")
    app.dependency_overrides[current_user] = lambda: {"id": "u1"}
    yield state
    app.dependency_overrides.clear()


def test_connect_requests_only_the_drive_file_scope_and_a_prefixed_single_use_state(store):
    url = client.post(URL + "/connect").json()["url"]
    assert "scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fdrive.file" in url
    assert "spreadsheets" not in url.replace("drive.file", "")
    assert store.states[0]["nonce"].startswith("sheets.") and ("state=" + store.states[0]["nonce"]) in url


def test_status_hides_the_spreadsheet_link_from_viewers(store, monkeypatch):
    assert client.get(URL).json()["spreadsheet_url"] == SAVED["spreadsheet_url"]
    monkeypatch.setattr(sheets, "membership", lambda *a, **k: "viewer")
    body = client.get(URL).json()
    assert body["connected"] is True and body["spreadsheet_url"] is None and body["can_manage"] is False


def test_viewers_cannot_connect_disconnect_or_send_test_rows(store, monkeypatch):
    def deny(*a, **k):
        raise HTTPException(403, "no")
    monkeypatch.setattr(sheets, "membership", deny)
    assert client.post(URL + "/connect").status_code == 403
    assert client.delete(URL).status_code == 403
    assert client.post(URL + "/test-row").status_code == 403
    assert store.rows == []


def test_export_appends_one_row_per_call_even_if_repeated(store):
    sheets.export_lead(WORKSPACE, "call1", "Ana", "+15551234567", "Quote")
    sheets.export_lead(WORKSPACE, "call1", "Ana", "+15551234567", "Quote")
    assert len(store.rows) == 1 and store.rows[0][1:] == ["Ana", "+15551234567", "Quote", "call1"]


def test_failed_append_never_raises_and_allows_a_later_retry(store):
    store.fail_append = True
    sheets.export_lead(WORKSPACE, "call2", "Bo", "+15551234567", "Install")
    assert store.exports == set() and store.rows == []
    store.fail_append = False
    sheets.export_lead(WORKSPACE, "call2", "Bo", "+15551234567", "Install")
    assert len(store.rows) == 1


def test_export_is_a_no_op_without_a_connection(store):
    store.saved = None
    sheets.export_lead(WORKSPACE, "call3", "Cy", "", "x")
    assert store.rows == [] and store.exports == set()


def test_test_row_needs_a_connection_and_is_clearly_labeled(store):
    assert client.post(URL + "/test-row").json() == {"added": True}
    assert store.rows[0][1] == "CallPilot test"
    store.saved = None
    assert client.post(URL + "/test-row").status_code == 409


def test_callback_for_sheets_state_rejects_missing_scope_and_never_stores_a_connection(store, monkeypatch):
    monkeypatch.setattr(sheets, "rpc", lambda *a, **k: [{"organization_id": WORKSPACE, "created_by": "u1"}])
    class Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"refresh_token": "r", "access_token": "a", "scope": "openid"}
    class Client:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def post(self, *a, **k): return Resp()
    monkeypatch.setattr(sheets.httpx, "Client", Client)
    store.saved = None
    cookie = hashlib.sha256(b"sheets.abc").hexdigest()
    response = client.get("/api/oauth/google/callback?code=c&state=sheets.abc", follow_redirects=False,
                          cookies={sheets.STATE_COOKIE: cookie})
    assert response.headers["location"] == "/app?sheets=missing_permission#connections"
    assert store.saved is None


def test_callback_without_the_browser_cookie_is_rejected_before_the_state_is_consumed(store, monkeypatch):
    consumed = []
    store.saved = None
    monkeypatch.setattr(sheets, "rpc", lambda *a, **k: consumed.append(a) or [{"organization_id": WORKSPACE, "created_by": "u1"}])
    for cookies in ({}, {sheets.STATE_COOKIE: "wrong"}):
        response = client.get("/api/oauth/google/callback?code=c&state=sheets.abc", follow_redirects=False, cookies=cookies)
        assert response.headers["location"] == "/app?sheets=error#connections"
    assert consumed == [] and store.saved is None


def test_callback_requires_the_initiator_to_still_be_an_admin(store, monkeypatch):
    monkeypatch.setattr(sheets, "rpc", lambda *a, **k: [{"organization_id": WORKSPACE, "created_by": "u1"}])
    def removed(*a, **k):
        raise HTTPException(404, "Workspace not found")
    monkeypatch.setattr(sheets, "membership", removed)
    cookie = hashlib.sha256(b"sheets.abc").hexdigest()
    response = client.get("/api/oauth/google/callback?code=c&state=sheets.abc", follow_redirects=False,
                          cookies={sheets.STATE_COOKIE: cookie})
    assert response.headers["location"] == "/app?sheets=error#connections"


def test_connect_sets_a_bound_httponly_cookie_and_disconnect_clears_pending_states(store):
    response = client.post(URL + "/connect")
    cookie = response.headers["set-cookie"].lower()
    assert sheets.STATE_COOKIE in cookie and "httponly" in cookie and "samesite=lax" in cookie
    deleted = []
    real = store.api
    store.api = lambda table, method="GET", **kw: deleted.append((table, method, kw.get("params"))) or real(table, method, **kw)
    import app.sheets as module
    module.api = store.api
    client.delete(URL)
    assert any(t == "oauth_states" and m == "DELETE" and p["nonce"] == "like.sheets.*" for t, m, p in deleted)


def test_calendar_states_still_use_the_calendar_flow(store, monkeypatch):
    from app import calendar
    monkeypatch.setattr(calendar, "rpc", lambda *a, **k: None)
    response = client.get("/api/oauth/google/callback?code=c&state=plainstate", follow_redirects=False)
    assert response.headers["location"] == "/app?calendar=expired#appointments"
