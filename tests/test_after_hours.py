"""Closed-hours greeting is chosen at admission and can never block a call."""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import after_hours
from app.account import current_user
from app.booking_hours import BookingSettings
from app.main import app

client = TestClient(app)
W = "00000000-0000-0000-0000-00000000000a"
GREETING = "Thanks for calling Metro. We are closed right now, but I can take a message."
WEEK = {"mon": [{"start": "09:00", "end": "17:00"}], "tue": [], "wed": [], "thu": [], "fri": [], "sat": [], "sun": []}


def settings(hours=True):
    return BookingSettings(timezone="America/New_York", hours={"week": WEEK, "exceptions": {}} if hours else None)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(after_hours, "get_settings", lambda w: settings())
    monkeypatch.setattr(after_hours, "closed_greeting", lambda w: GREETING)
    return monkeypatch


MONDAY_NOON_NY = datetime(2026, 11, 9, 17, 0, tzinfo=timezone.utc)   # 12:00 New York
MONDAY_NIGHT_NY = datetime(2026, 11, 10, 3, 0, tzinfo=timezone.utc)  # 22:00 New York


def test_open_and_closed_instants(env):
    assert after_hours.is_open_now(W, MONDAY_NOON_NY) is True
    assert after_hours.is_open_now(W, MONDAY_NIGHT_NY) is False


def test_no_hours_configured_means_always_open(env):
    env.setattr(after_hours, "get_settings", lambda w: settings(hours=False))
    assert after_hours.is_open_now(W, MONDAY_NIGHT_NY) is True


def test_closed_call_gets_greeting_override_with_both_hooks(env):
    env.setattr(after_hours, "is_open_now", lambda w: False)
    result = after_hours.apply_after_hours(W, "asst", {"assistantId": "asst"})
    overrides = result["assistantOverrides"]
    assert overrides["firstMessage"] == GREETING
    assert [h["do"][0]["exact"] for h in overrides["hooks"]][0] == GREETING and len(overrides["hooks"]) == 2


def test_open_call_and_other_assistants_are_untouched(env):
    env.setattr(after_hours, "is_open_now", lambda w: True)
    assert after_hours.apply_after_hours(W, "asst", {"assistantId": "asst"}) == {"assistantId": "asst"}
    env.setattr(after_hours, "is_open_now", lambda w: False)
    assert after_hours.apply_after_hours(W, "asst", {"assistantId": "worker"}) == {"assistantId": "worker"}
    assert after_hours.apply_after_hours(W, "asst", {"destination": {"type": "number"}}) == {"destination": {"type": "number"}}


def test_no_greeting_set_or_errors_never_block_the_call(env):
    env.setattr(after_hours, "is_open_now", lambda w: False)
    env.setattr(after_hours, "closed_greeting", lambda w: None)
    assert after_hours.apply_after_hours(W, "asst", {"assistantId": "asst"}) == {"assistantId": "asst"}
    def boom(w):
        raise RuntimeError("database down")
    env.setattr(after_hours, "closed_greeting", boom)
    assert after_hours.apply_after_hours(W, "asst", {"assistantId": "asst"}) == {"assistantId": "asst"}


def test_save_validates_and_requires_admin(env):
    saved = []
    env.setattr(after_hours, "membership", lambda *a, **k: "owner")
    env.setattr(after_hours, "api", lambda table, method="GET", params=None, data=None, **k: saved.append(data) or [])
    app.dependency_overrides[current_user] = lambda: {"id": "u"}
    try:
        url = "/api/account/organizations/%s/after-hours" % W
        assert client.put(url, json={"closed_greeting": "short"}).status_code == 422
        assert client.put(url, json={"closed_greeting": "  "}).json() == {"closed_greeting": None}
        assert client.put(url, json={"closed_greeting": GREETING}).json()["closed_greeting"] == GREETING
        assert saved == [{"closed_greeting": None}, {"closed_greeting": GREETING}]
        from fastapi import HTTPException
        def deny(*a, **k):
            raise HTTPException(404, "Workspace not found")
        env.setattr(after_hours, "membership", deny)
        assert client.put(url, json={"closed_greeting": GREETING}).status_code == 404
    finally:
        app.dependency_overrides.clear()
