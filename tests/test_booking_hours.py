from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import account, booking_hours, calendar
from app.booking_hours import BookingSettings, slot_allowed
from app.main import app

OID = "123e4567-e89b-12d3-a456-426614174000"
BASE = f"/api/account/organizations/{OID}"


def test_split_hours_and_date_override_use_business_timezone():
    settings = BookingSettings(timezone="America/New_York", hours={
        "week": {"mon": [{"start": "09:00", "end": "12:00"}, {"start": "13:00", "end": "17:00"}]},
        "exceptions": {"2026-11-16": [], "2026-11-23": [{"start": "10:00", "end": "12:00"}]},
    })

    def allowed(start, minutes):
        instant = datetime.fromisoformat(start)
        return slot_allowed(settings, instant, instant + timedelta(minutes=minutes))

    assert allowed("2026-11-09T16:30:00+00:00", 30)
    assert not allowed("2026-11-09T16:45:00+00:00", 30)
    assert not allowed("2026-11-16T15:00:00+00:00", 30)
    assert not allowed("2026-11-23T14:00:00+00:00", 30)
    assert allowed("2026-11-23T15:00:00+00:00", 30)


def test_booking_hours_distinguishes_closed_schedule_from_unconfigured():
    start = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)
    end = start + timedelta(minutes=30)
    assert slot_allowed(BookingSettings(timezone="UTC"), start, end)
    assert not slot_allowed(BookingSettings(timezone="UTC", hours={}), start, end)


def test_overlap_and_invalid_timezone_are_rejected():
    with pytest.raises(ValidationError, match="must not overlap"):
        BookingSettings(timezone="UTC", hours={
            "week": {"mon": [{"start": "09:00", "end": "12:00"}, {"start": "11:00", "end": "15:00"}]},
        })
    with pytest.raises(ValidationError, match="valid time zone"):
        BookingSettings(timezone="Not/AZone")


def test_dst_fall_back_checks_both_occurrences():
    settings = BookingSettings(timezone="America/New_York", hours={
        "week": {"sun": [{"start": "01:00", "end": "01:45"}]},
    })
    for hour in [5, 6]:
        start = datetime(2026, 11, 1, hour, 10, tzinfo=timezone.utc)
        assert slot_allowed(settings, start, start + timedelta(minutes=20))


def test_malformed_calendar_reply_never_counts_as_free(monkeypatch):
    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, *args, **kwargs):
            return calendar.httpx.Response(200, json={"calendars": {}})

    monkeypatch.setattr(calendar.httpx, "Client", Client)
    start = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)
    with pytest.raises(HTTPException, match="Calendar availability unavailable"):
        calendar.free_busy("test", start, start + timedelta(minutes=30))


def test_hours_are_tenant_scoped_and_checked_before_calendar(monkeypatch):
    def member(oid, user, roles=None):
        assert oid == OID
        return "owner"

    db = []

    def database(table, method="GET", **kwargs):
        assert table == "organizations"
        assert kwargs["params"]["id"] == f"eq.{OID}"
        db.append((method, kwargs))
        return [{"timezone": "UTC", "booking_hours": {"week": {}, "exceptions": {}}}]

    app.dependency_overrides[account.current_user] = lambda: {"id": "qa"}
    monkeypatch.setattr(booking_hours, "membership", member)
    monkeypatch.setattr(calendar, "membership", member)
    monkeypatch.setattr(booking_hours, "api", database)
    monkeypatch.setattr(calendar, "google_token", lambda oid: pytest.fail("No calendar call for a closed date"))
    try:
        client = TestClient(app)
        assert client.get(BASE + "/booking-hours").json()["timezone"] == "UTC"
        changed = {"timezone": "America/New_York", "hours": {"week": {}, "exceptions": {}}}
        assert client.put(BASE + "/booking-hours", json=changed).json() == changed
        assert db[-1][1]["data"]["booking_hours"] == changed["hours"]
        future = datetime.now(timezone.utc) + timedelta(days=1)
        body = {"start": future.isoformat(), "duration_minutes": 30}
        assert client.post(BASE + "/calendar/availability", json=body).json()["available"] is False
        booking = {**body, "caller_name": "Example Person", "caller_contact": "qa@example.com", "service": "Test"}
        assert client.post(BASE + "/calendar/book-test", json=booking).status_code == 409
    finally:
        app.dependency_overrides.pop(account.current_user, None)
