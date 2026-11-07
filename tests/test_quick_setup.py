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


def test_transfer_number_is_saved_once_or_requested(make, monkeypatch):
    env = make()
    monkeypatch.setattr("app.transfer_directory.reject_loops", lambda w, d: None)
    client.post(URL, json={"goals": ["transfer"], "transfer_number": "+12125550100", "transfer_label": "Front desk"})
    assert env.patched[0]["transfer_directory"][0]["number"] == "+12125550100"
    env = make()
    result = client.post(URL, json={"goals": ["transfer"]}).json()
    assert any(n["key"] == "transfer" for n in result["needs"]) and "transfer_directory" not in env.patched[0]


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
