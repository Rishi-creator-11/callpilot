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


def test_booked_calendar_event_is_persisted(monkeypatch):
    """Offline e2e of reservation -> Google Calendar -> confirmed DB event."""
    import app.calendar as calendar
    import app.account as account
    monkeypatch.setattr(voice_actions, "feature_flags", lambda oid: flags(voice_booking_enabled=True))
    monkeypatch.setattr(calendar, "google_token", lambda oid: "fake-calendar-token")
    monkeypatch.setattr(calendar, "free_busy", lambda token, a, b: True)
    monkeypatch.setattr(calendar, "within_booking_hours", lambda oid, a, b: True)
    monkeypatch.setattr(account, "rpc", lambda name, args: [{
        "reservation_status": "reserved",
        "reservation_id": "00000000-0000-4000-8000-000000000001",
    }])
    updates=[]
    monkeypatch.setattr(voice_actions, "api", lambda table, method="GET", params=None,
                        data=None, prefer=None: updates.append((table, method, data)) or [])
    class Response:
        def __init__(self, status, data=None):
            self.status_code = status
            self._data = data or {}
        def json(self):
            return self._data
    class FakeClient:
        def __init__(self, timeout):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get(self, url, headers):
            return Response(404)
        def post(self, url, headers, json):
            assert json["extendedProperties"]["private"]["callpilot_org"] == OID
            return Response(201, {"id": json["id"]})
    monkeypatch.setattr(voice_actions.httpx, "Client", FakeClient)
    future = (datetime.now(timezone.utc) + timedelta(days=3)).replace(microsecond=0)
    reply = voice_actions.book_appointment(OID, CALL, {
        "confirmed": True, "start": future.isoformat(),
        "duration_minutes": 30, "caller_name": "Sam Taylor",
        "caller_contact": "+15555550100", "service": "Business card consultation"})
    assert "Confirmed Google Calendar appointment" in reply
    assert any(t == "appointment_requests" and method == "PATCH" and
               data["status"] == "confirmed" and data["calendar_event_id"].startswith("cp")
               for t, method, data in updates)


def test_conflicting_reservation_does_not_create_event(monkeypatch):
    import app.account as account
    monkeypatch.setattr(voice_actions, "feature_flags", lambda oid: flags(voice_booking_enabled=True))
    monkeypatch.setattr(account, "rpc", lambda name, args: [{
        "reservation_status": "conflict", "reservation_id": None}])
    future = (datetime.now(timezone.utc) + timedelta(days=3)).replace(microsecond=0)
    reply = voice_actions.book_appointment(OID, CALL, {
        "confirmed": True, "start": future.isoformat(), "duration_minutes": 30,
        "caller_name": "Sam Taylor", "caller_contact": "+15555550100",
        "service": "Consultation"})
    assert "another customer" in reply


@pytest.mark.parametrize("value", ["914446046", "4460465", "12345", "+1 914 446"])
def test_incomplete_callback_numbers_are_refused_with_a_helpful_message(value):
    with pytest.raises(ValueError, match="full number slowly|Invalid callback phone format"):
        voice_actions._phone(value)


@pytest.mark.parametrize("value", ["9144460465", "(914) 446-0465", "+1 914 446 0465", "+44 20 7946 0958"])
def test_complete_callback_numbers_are_accepted(value):
    assert voice_actions._phone(value) == value


def test_a_call_without_a_phone_number_gets_a_plain_instruction(monkeypatch):
    monkeypatch.setattr(voice_actions, "feature_flags", lambda oid: {"voice_leads_enabled": True, "provider_agent_id": "asst"})
    monkeypatch.setattr(voice_actions, "_call_context", lambda oid, message: ("call-1", {"customer": {}}))
    monkeypatch.setattr(voice_actions, "api", lambda *a, **k: [])
    with pytest.raises(ValueError, match="don't have their number from this call"):
        voice_actions.capture_customer_lead("o", {}, {"caller_name": "Rishi", "interest": "200 business cards",
                                                      "callback_phone": "", "callback_requested": True})


def _store(monkeypatch, **extra):
    writes = []
    monkeypatch.setattr(voice_actions, "feature_flags", lambda oid: flags(**extra))
    monkeypatch.setattr(voice_actions, "api", lambda table, method="GET", **kw: writes.append((table, kw.get("data"))) or [{"id": "new-id"}])
    return writes


def test_orders_are_taken_in_the_callers_words_when_there_is_no_catalog(monkeypatch):
    tools = voice_actions.voice_tool_definitions(OID, "c", flags(voice_order_requests_enabled=True))
    assert [t["function"]["name"] for t in tools] == ["capture_order_request"]
    writes = _store(monkeypatch, voice_order_requests_enabled=True)
    args = {"product": "Business cards", "quantity": "500", "details": "3.5x2, matte, pickup Friday",
            "caller_name": "Dana Lee", "caller_contact": "5555550123", "confirmed": True}
    result = voice_actions.capture_order_request(OID, CALL, args)
    table, data = writes[0]
    assert table == "order_requests" and data["product_text"] == "Business cards" and data["quantity"] == 500
    assert data["status"] == "requested" and "not confirmed or paid" in result
    with pytest.raises(ValueError):
        voice_actions.capture_order_request(OID, CALL, {**args, "confirmed": False})


def test_appointment_request_is_saved_unconfirmed_when_no_calendar_is_connected(monkeypatch):
    tools = voice_actions.voice_tool_definitions(OID, "c", flags(voice_leads_enabled=True))
    assert "request_appointment" in [t["function"]["name"] for t in tools]
    booked = voice_actions.voice_tool_definitions(OID, "c", flags(voice_leads_enabled=True, voice_booking_enabled=True))
    assert "request_appointment" not in [t["function"]["name"] for t in booked]
    writes = _store(monkeypatch, voice_leads_enabled=True)
    when = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    result = voice_actions.request_appointment(OID, CALL, {
        "service": "Consultation", "preferred_time": when, "caller_name": "Dana Lee",
        "caller_contact": "5555550123", "notes": ""})
    table, data = writes[0]
    assert table == "appointment_requests" and data["status"] == "requested" and "NOT confirmed" in result
    with pytest.raises(ValueError):
        voice_actions.request_appointment(OID, CALL, {
            "service": "Consultation", "preferred_time": "2020-01-01T10:00:00+00:00", "caller_name": "Dana Lee",
            "caller_contact": "5555550123", "notes": ""})
