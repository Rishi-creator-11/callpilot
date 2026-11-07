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
        assert args == {"p_organization_id": OID, "p_user_id": OTHER}
        return True

    def provider(method, path, *, body):
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
