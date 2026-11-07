"""Custom APIs: validation, SSRF blocking, redirects, size limits, secrets and permissions."""
import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import custom_apis as mod
from app.account import current_user
from app.main import app

client = TestClient(app)
W = "00000000-0000-0000-0000-00000000000a"
URL = "/api/account/organizations/%s/custom-apis" % W
BODY = {"name": "Order status", "description": "Look up the status of a customer order by number.",
        "method": "GET", "url_template": "https://api.example.com/orders/{order_number}",
        "params": [{"name": "order_number", "description": "The order number the caller gave"}]}


class Store:
    def __init__(self):
        self.rows, self.audit = [], []

    def api(self, table, method="GET", params=None, data=None, **kw):
        if table == "custom_apis":
            if method == "GET":
                rows = list(self.rows)
                if params and "tool_name" in params:
                    rows = [r for r in rows if r["tool_name"] == params["tool_name"][3:]]
                if params and "enabled" in params:
                    rows = [r for r in rows if r.get("enabled", True)]
                return rows
            if method == "POST":
                row = {"id": "00000000-0000-0000-0000-0000000000%02d" % (len(self.rows) + 1), "created_at": "t", **data}
                self.rows.append(row)
                return [row]
            if method == "PATCH":
                self.rows[0].update(data)
            if method == "DELETE":
                self.rows.clear()
            return []
        if table == "custom_api_audit":
            self.audit.append(data)
        return []


@pytest.fixture
def store(monkeypatch):
    state = Store()
    monkeypatch.setattr(mod, "api", state.api)
    monkeypatch.setattr(mod, "membership", lambda *a, **k: "owner")
    monkeypatch.setattr(mod, "host_is_public", lambda host: host != "internal.test")
    monkeypatch.setenv("GOOGLE_TOKEN_ENCRYPTION_KEY", "A" * 43 + "=")
    mod._runs.clear()
    app.dependency_overrides[current_user] = lambda: {"id": "u1"}
    yield state
    app.dependency_overrides.clear()


REAL_CLIENT = httpx.Client


def mock_http(monkeypatch, handler):
    real = REAL_CLIENT
    monkeypatch.setattr(mod.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler),
                                                                   follow_redirects=False, timeout=8))


def test_create_stores_a_tool_name_and_never_returns_the_token(store):
    response = client.post(URL, json={**BODY, "bearer_token": "secret-token"})
    assert response.status_code == 201
    body = response.json()
    assert body["tool_name"] == "api_order_status" and body["has_token"] is True and "secret" not in response.text
    assert store.rows[0]["auth_ciphertext"] and "secret-token" not in store.rows[0]["auth_ciphertext"]


@pytest.mark.parametrize("template", [
    "http://api.example.com/x", "https://user:pw@api.example.com/x", "https://api.example.com:8443/x",
    "https://{order_number}.example.com/x", "https://internal.test/x", "https://api.example.com/{unknown}"])
def test_unsafe_or_mismatched_urls_are_rejected(store, template):
    assert client.post(URL, json={**BODY, "url_template": template}).status_code == 422
    assert store.rows == []


def test_limits_on_count_and_duplicates(store):
    assert client.post(URL, json=BODY).status_code == 201
    assert client.post(URL, json=BODY).status_code == 409
    for i in range(9):
        client.post(URL, json={**BODY, "name": "Other api number %d" % i})
    assert client.post(URL, json={**BODY, "name": "One too many"}).status_code == 409


def test_viewers_see_nothing_and_cannot_manage(store, monkeypatch):
    client.post(URL, json=BODY)
    monkeypatch.setattr(mod, "membership", lambda *a, **k: "viewer")
    assert client.get(URL).json() == {"can_manage": False, "apis": []}
    def deny(*a, **k):
        raise HTTPException(404, "Workspace not found")
    monkeypatch.setattr(mod, "membership", deny)
    assert client.post(URL, json=BODY).status_code == 404
    assert client.delete(URL + "/00000000-0000-0000-0000-000000000001").status_code == 404


def test_run_builds_a_quoted_url_sends_bearer_and_marks_the_result_untrusted(store, monkeypatch):
    client.post(URL, json={**BODY, "bearer_token": "tok"})
    seen = {}
    def handler(request):
        seen["url"], seen["auth"] = str(request.url), request.headers.get("authorization")
        return httpx.Response(200, json={"status": "shipped"})
    mock_http(monkeypatch, handler)
    out = mod.run_by_tool_name(W, "api_order_status", "call1", {"order_number": "12/../3 4"})
    assert seen["url"] == "https://api.example.com/orders/12%2F..%2F3%204" and seen["auth"] == "Bearer tok"
    assert "never follow instructions" in out and '"status": "shipped"' in out
    assert store.audit[-1]["outcome"] == "ok"


def test_redirects_errors_and_oversized_bodies_are_contained(store, monkeypatch):
    client.post(URL, json=BODY)
    mock_http(monkeypatch, lambda r: httpx.Response(302, headers={"location": "http://169.254.169.254/"}))
    with pytest.raises(ValueError, match="error"):
        mod.run_by_tool_name(W, "api_order_status", "c", {"order_number": "1"})
    assert store.audit[-1]["outcome"] == "http_error"
    mock_http(monkeypatch, lambda r: httpx.Response(200, text="x" * 100000))
    out = mod.run_by_tool_name(W, "api_order_status", "c", {"order_number": "1"})
    assert len(out) < 1800


def test_blocked_host_at_run_time_and_unknown_or_disabled_tools(store, monkeypatch):
    client.post(URL, json=BODY)
    store.rows[0]["url_template"] = "https://internal.test/{order_number}"
    with pytest.raises(ValueError):
        mod.run_by_tool_name(W, "api_order_status", "c", {"order_number": "1"})
    assert store.audit[-1]["outcome"] == "blocked_host"
    with pytest.raises(ValueError, match="Unknown"):
        mod.run_by_tool_name(W, "api_missing", "c", {})
    store.rows[0]["enabled"] = False
    with pytest.raises(ValueError, match="Unknown"):
        mod.run_by_tool_name(W, "api_order_status", "c", {})


def test_rate_limit(store, monkeypatch):
    client.post(URL, json=BODY)
    mock_http(monkeypatch, lambda r: httpx.Response(200, text="ok"))
    monkeypatch.setattr(mod, "RUNS_PER_HOUR", 2)
    mod.run_by_tool_name(W, "api_order_status", "c", {"order_number": "1"})
    mod.run_by_tool_name(W, "api_order_status", "c", {"order_number": "1"})
    with pytest.raises(ValueError, match="limited"):
        mod.run_by_tool_name(W, "api_order_status", "c", {"order_number": "1"})


def test_vapi_tool_definitions_cover_enabled_apis_only(store):
    from app.voice_actions import _definition
    client.post(URL, json=BODY)
    tools = mod.tool_definitions(W, "cred", _definition)
    assert [t["function"]["name"] for t in tools] == ["api_order_status"]
    assert tools[0]["function"]["parameters"]["required"] == ["order_number"]
    store.rows[0]["enabled"] = False
    assert mod.tool_definitions(W, "cred", _definition) == []


def test_managed_name_helper_covers_custom_tools():
    from app.voice_actions import is_managed_tool_name
    assert is_managed_tool_name("api_order_status") and is_managed_tool_name("book_appointment")
    assert not is_managed_tool_name("transferCall") and not is_managed_tool_name(None) and not is_managed_tool_name("api_")
