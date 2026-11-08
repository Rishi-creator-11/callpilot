import pytest
from fastapi.testclient import TestClient

from app import account, voice_preview
from app.main import app

OID = "123e4567-e89b-12d3-a456-426614174000"
OTHER = "123e4567-e89b-12d3-a456-426614174001"
START = f"/api/account/organizations/{OID}/voice-preview"


@pytest.fixture
def preview_client(monkeypatch):
    app.dependency_overrides[account.current_user] = lambda: {"id": OTHER}
    monkeypatch.setattr(voice_preview, "membership", lambda *args: "owner")
    monkeypatch.setenv("VOICE_PREVIEW_ENABLED", "true")
    monkeypatch.setenv("VAPI_API_KEY", "test")
    monkeypatch.setenv("VAPI_WEBHOOK_TOKEN", "test-webhook")
    monkeypatch.setattr(voice_preview, "org_or_404", lambda oid: {"name": "Practice business"})
    monkeypatch.setattr(voice_preview, "api", lambda *args, **kwargs: [
        {"agent_name": "Avery", "greeting": "Hello from the front desk."},
    ])
    monkeypatch.setattr("app.voice_actions.feature_flags", lambda oid: {
        "voice_booking_enabled": True, "voice_leads_enabled": True,
        "voice_catalog_enabled": True, "voice_order_requests_enabled": True,
    })
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(account.current_user, None)


def test_start_preview_only_creates_read_only_web_assistant(preview_client, monkeypatch):
    def reserve(name, args):
        assert name == "reserve_voice_preview"
        assert args == {"p_organization_id": OID, "p_user_id": OTHER, "p_daily_cap": 100}
        return True

    def provider(method, path, *, body, key_env=None):
        assert (method, path) == ("POST", "/call/web")
        assert body["roomDeleteOnUserLeaveEnabled"] is True
        assert "customer" not in body and "phoneNumberId" not in body and "assistantId" not in body
        assistant = body["assistant"]
        assert assistant["maxDurationSeconds"] == 90
        assert assistant["artifactPlan"]["recordingEnabled"] is False
        assert assistant["artifactPlan"]["transcriptPlan"]["enabled"] is False
        assert assistant["transcriber"]["language"] == "multi"
        knowledge, end = assistant["model"]["tools"]
        assert knowledge["function"]["name"] == "lookup_approved_knowledge"
        assert knowledge["server"]["url"].endswith(f"/webhooks/vapi-preview/{OID}")
        assert end == {"type": "endCall"}
        prompt = assistant["model"]["messages"][0]["content"]
        assert "This is a practice call" in prompt
        assert "passing confirmed true" not in prompt
        return {"id": "practice", "webCallUrl": "https://example.daily.co/room",
                "assistant": assistant, "transport": {"callToken": "room-token"}}

    monkeypatch.setattr(voice_preview, "rpc", reserve)
    monkeypatch.setattr(voice_preview, "vapi", provider)
    response = preview_client.post(START)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"id": "practice", "webCallUrl": "https://example.daily.co/room",
                               "transport": {"callToken": "room-token"}}


def test_disabled_or_rate_limited_previews_do_not_contact_provider(preview_client, monkeypatch):
    monkeypatch.setattr(voice_preview, "vapi", lambda *args, **kwargs: pytest.fail("Provider must not be called"))
    monkeypatch.setattr(voice_preview, "rpc", lambda *args: False)
    assert preview_client.post(START).status_code == 429
    monkeypatch.delenv("VOICE_PREVIEW_ENABLED")
    assert preview_client.get(START).json() == {"available": False}
    assert preview_client.post(START).status_code == 503


def test_invalid_provider_room_is_rejected(preview_client, monkeypatch):
    monkeypatch.setattr(voice_preview, "rpc", lambda *args: True)
    monkeypatch.setattr(voice_preview, "vapi", lambda *args, **kwargs: {
        "webCallUrl": "https://example.daily.co.evil.example/room",
    })
    assert preview_client.post(START).status_code == 502


def test_preview_webhook_is_tenant_bound_and_has_no_action_tools(preview_client, monkeypatch):
    monkeypatch.setattr(voice_preview, "answer_question", lambda oid, question: {
        "answer": "Open Tuesday.", "source": "approved_semantic",
    } if oid == OID else pytest.fail("Wrong tenant"))
    header = {"Authorization": "Bearer " + voice_preview.preview_token(OID)}
    payload = {"message": {"type": "tool-calls", "toolCallList": [
        {"id": "read", "function": {"name": "lookup_approved_knowledge", "arguments": '{"question":"Open when?"}'}},
        {"id": "write", "function": {"name": "book_appointment", "arguments": {}}},
    ]}}
    wrong = preview_client.post(f"/webhooks/vapi-preview/{OTHER}", json=payload, headers=header)
    assert wrong.status_code == 401
    results = preview_client.post(f"/webhooks/vapi-preview/{OID}", json=payload, headers=header).json()["results"]
    assert results[0] == {"toolCallId": "read", "result": "Open Tuesday."}
    assert "Only approved knowledge" in results[1]["error"]


def test_voice_is_available_to_every_workspace_when_the_server_switch_is_on(preview_client, monkeypatch):
    monkeypatch.setenv("VOICE_PREVIEW_WORKSPACES", OTHER)  # a leftover allowlist no longer limits anyone
    assert preview_client.get(START).json() == {"available": True}
    monkeypatch.setenv("VOICE_PREVIEW_ENABLED", "false")
    assert preview_client.get(START).json() == {"available": False}


LIVE = f"/api/account/organizations/{OID}/voice-test"


def test_live_test_calls_the_workspaces_real_assistant_only(preview_client, monkeypatch):
    monkeypatch.setattr(voice_preview, "api", lambda *a, **k: [{"provider_agent_id": "asst_1"}])
    monkeypatch.setattr("app.call_intel.owns_assistant", lambda assistant, oid: assistant.get("id") == "asst_1" and oid == OID)
    monkeypatch.setattr(voice_preview, "rpc", lambda name, args: True)
    calls = []

    def provider(method, path, *, body=None, key_env=None):
        calls.append((method, path, body))
        if method == "GET":
            return {"id": "asst_1"}
        return {"webCallUrl": "https://callpilot.daily.co/room", "id": "call_1"}

    monkeypatch.setattr(voice_preview, "vapi", provider)
    response = preview_client.post(LIVE)
    assert response.status_code == 200 and response.json()["webCallUrl"] == "https://callpilot.daily.co/room"
    assert response.headers["cache-control"] == "no-store"
    create = calls[-1]
    assert create[1] == "/call/web" and create[2]["assistantId"] == "asst_1" and "assistant" not in create[2]
    assert create[2]["assistantOverrides"]["maxDurationSeconds"] == 150


def test_live_test_needs_an_agent_ownership_and_quota_before_contacting_the_provider(preview_client, monkeypatch):
    provider_calls = []
    monkeypatch.setattr(voice_preview, "vapi", lambda *a, **k: provider_calls.append(a) or {"id": "asst_1"})
    monkeypatch.setattr(voice_preview, "api", lambda *a, **k: [{"provider_agent_id": None}])
    assert preview_client.post(LIVE).status_code == 409 and provider_calls == []
    monkeypatch.setattr(voice_preview, "api", lambda *a, **k: [{"provider_agent_id": "asst_1"}])
    monkeypatch.setattr("app.call_intel.owns_assistant", lambda assistant, oid: False)
    assert preview_client.post(LIVE).status_code == 409
    monkeypatch.setattr("app.call_intel.owns_assistant", lambda assistant, oid: True)
    monkeypatch.setattr(voice_preview, "rpc", lambda name, args: False)
    assert preview_client.post(LIVE).status_code == 429
    assert not any(call[0] == "POST" for call in provider_calls)  # no call room was ever created


def test_live_test_is_off_when_voice_calls_are_disabled(preview_client, monkeypatch):
    monkeypatch.setenv("VOICE_PREVIEW_ENABLED", "false")
    assert preview_client.post(LIVE).status_code == 503


def test_the_shared_daily_cap_is_configurable_and_bounded(monkeypatch):
    monkeypatch.delenv("VOICE_TEST_DAILY_CAP", raising=False)
    assert voice_preview.daily_cap() == 100
    monkeypatch.setenv("VOICE_TEST_DAILY_CAP", "250")
    assert voice_preview.daily_cap() == 250
    monkeypatch.setenv("VOICE_TEST_DAILY_CAP", "999999")
    assert voice_preview.daily_cap() == 5000
    monkeypatch.setenv("VOICE_TEST_DAILY_CAP", "nonsense")
    assert voice_preview.daily_cap() == 100


def test_a_failed_call_does_not_use_up_the_allowance(preview_client, monkeypatch):
    released = []
    monkeypatch.setattr(voice_preview, "rpc", lambda name, args: True)
    def api(table, method="GET", params=None, **kw):
        if method == "DELETE":
            released.append((table, params["organization_id"], params["user_id"]))
            return []
        return [{"provider_agent_id": "asst_1"}]
    monkeypatch.setattr(voice_preview, "api", api)
    monkeypatch.setattr("app.call_intel.owns_assistant", lambda assistant, oid: True)
    from fastapi import HTTPException
    def provider(method, path, *, body=None, key_env=None):
        if method == "GET":
            return {"id": "asst_1"}
        raise HTTPException(502, "Vapi request failed (HTTP 401)")
    monkeypatch.setattr(voice_preview, "vapi", provider)
    assert preview_client.post(LIVE).status_code == 502
    assert released == [("voice_preview_attempts", "eq." + OID, "eq." + OTHER)]
