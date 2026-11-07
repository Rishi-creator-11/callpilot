"""Offline unit tests: no real Vapi calls, caller contact writes, or calendar events."""
from datetime import datetime, timedelta, timezone

import pytest

from app import voice_actions


OID = "b550c0af-7d9c-4231-8eec-cb9f6ba9e724"
ASSISTANT = "test-managed-assistant"
CALL = {"call": {"id": "call-test-1234", "assistantId": ASSISTANT,
                 "type": "inboundPhoneCall", "customer": {"number": "+15555550100"}}}


def flags(**extra):
    return {"provider_agent_id": ASSISTANT,
            "voice_leads_enabled": False, "voice_booking_enabled": False, **extra}


def test_tools_are_opt_in():
    assert voice_actions.voice_tool_definitions(OID, "private-credential", flags()) == []
    tools = voice_actions.voice_tool_definitions(
        OID, "private-credential", flags(voice_leads_enabled=True, voice_booking_enabled=True))
    assert [t["function"]["name"] for t in tools] == [
        "capture_customer_lead", "check_appointment_availability", "book_appointment"]
    assert all(t["server"]["url"].endswith(OID) for t in tools)


def test_wrong_tenant_never_receives_call_data(monkeypatch):
    monkeypatch.setattr(voice_actions, "feature_flags", lambda oid: flags(provider_agent_id="different"))
    with pytest.raises(ValueError, match="does not belong"):
        voice_actions.capture_customer_lead(OID, CALL, {
            "caller_name": "Sam", "callback_phone": "+15555550100",
            "interest": "business cards", "callback_requested": True})


def test_no_silent_callback_capture(monkeypatch):
    monkeypatch.setattr(voice_actions, "feature_flags", lambda oid: flags(voice_leads_enabled=True))
    with pytest.raises(ValueError, match="not agreed"):
        voice_actions.capture_customer_lead(OID, CALL, {
            "caller_name": "Sam", "callback_phone": "+15555550100",
            "interest": "business cards", "callback_requested": False})


def test_lead_saved_without_clobbering_review(monkeypatch):
    calls = []
    monkeypatch.setattr(voice_actions, "feature_flags", lambda oid: flags(voice_leads_enabled=True))
    def api(table, method="GET", params=None, data=None, prefer=None):
        calls.append((table, method, data))
        return ([{"id": "existing", "lead_source": "human_reviewed"}]
                if method == "GET" else [])
    monkeypatch.setattr(voice_actions, "api", api)
    response = voice_actions.capture_customer_lead(OID, CALL, {
        "caller_name": "Sam Taylor", "callback_phone": "+15555550200",
        "interest": "business card printing", "callback_requested": True})
    assert "saved" in response
    patch = calls[-1][2]
    assert patch["caller_name"] == "Sam Taylor"
    assert patch["follow_up_needed"] is True
    assert "lead_source" not in patch


def test_appointment_requires_real_confirmation(monkeypatch):
    monkeypatch.setattr(voice_actions, "feature_flags", lambda oid: flags(voice_booking_enabled=True))
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    with pytest.raises(ValueError, match="explicit caller agreement"):
        voice_actions.book_appointment(OID, CALL, {
            "confirmed": False, "start": tomorrow, "duration_minutes": 30,
            "caller_name": "Sam", "caller_contact": "+15555550100", "service": "consultation"})


def test_slot_requires_timezone_and_valid_duration():
    future = (datetime.now(timezone.utc) + timedelta(days=3)).replace(microsecond=0)
    with pytest.raises(ValueError, match="timezone"):
        voice_actions._slot({"start": future.replace(tzinfo=None).isoformat(),
                             "duration_minutes": 30})
    with pytest.raises(Exception):
        voice_actions._slot({"start": future.isoformat(), "duration_minutes": 999})
    a, b, minutes = voice_actions._slot({"start": future.isoformat(), "duration_minutes": 45})
    assert (b-a).total_seconds() == 2700 and minutes == 45


def test_deterministic_calendar_event_ids_are_valid():
    a = voice_actions._event_id("same-idempotency")
    assert a == voice_actions._event_id("same-idempotency")
    assert a != voice_actions._event_id("different")
    assert a.startswith("cp") and set(a) <= set("0123456789abcdefp")


def test_tool_action_fails_without_feature_flag(monkeypatch):
    monkeypatch.setattr(voice_actions, "feature_flags", lambda oid: flags())
    with pytest.raises(ValueError, match="not been enabled"):
        voice_actions.check_appointment_availability(OID, CALL, {
            "start": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
            "duration_minutes": 30})
