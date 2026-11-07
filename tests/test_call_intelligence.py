"""Regression checks for trustworthy lead intelligence and workspace isolation."""
from fastapi.testclient import TestClient
from app.call_intel import sanitize, classify, persist
from app.main import app
import app.call_intel as ci

client=TestClient(app)

def test_calls_require_login():
    for route in ("/api/account/organizations/00000000-0000-0000-0000-000000000001/calls",
                  "/api/account/organizations/00000000-0000-0000-0000-000000000001/calls/00000000-0000-0000-0000-000000000002"):
        assert client.get(route).status_code==401

def test_uncertain_call_is_not_made_hot():
    r=sanitize({"id":"call_123","type":"inboundPhoneCall",
        "startedAt":"2026-11-06T06:10:00Z","endedAt":"2026-11-06T06:12:00Z",
        "artifact":{"transcript":"User: Hello\nAI: Hi",
                    "recordingUrl":"https://recording.example.net/recording.wav"},
        "analysis":{"summary":"Customer made a general inquiry."}},
        "00000000-0000-0000-0000-000000000001")
    assert r["duration_seconds"]==120
    assert r["lead_stage"]=="needs_review"
    assert r["lead_source"]=="unreviewed"
    assert r["transcript"].startswith("User:")
    assert r["recording_url"].startswith("https://")

def test_structured_hot_requires_evidence():
    assert classify({"lead_stage":"hot"})[0]=="needs_review"
    assert classify({"lead_stage":"hot","reason":"Caller asked to purchase 2 full containers next week"})[0]=="hot"
    assert classify({"lead_stage":"verified","reason":"Caller said they are legit"})[0]=="needs_review"

def test_sync_preserves_human_review(monkeypatch):
    captured=[]
    def fake_api(table,method="GET",params=None,data=None,prefer=None):
        if method=="GET":
            return [{"id":"row-id","lead_source":"human_reviewed","lead_stage":"warm",
              "lead_reason":"Reviewed transcript","follow_up_needed":True,
              "follow_up_status":"todo","follow_up_note":"Call tomorrow",
              "follow_up_due_at":None,"last_reviewed_by":None}]
        captured.append(data)
        return []
    monkeypatch.setattr(ci,"api",fake_api)
    persist("00000000-0000-0000-0000-000000000001",{
        "id":"call_123","type":"inboundPhoneCall","startedAt":"2026-11-06T06:10:00Z"})
    assert captured[0]["lead_source"]=="human_reviewed"
    assert captured[0]["lead_stage"]=="warm"
    assert captured[0]["follow_up_status"]=="todo"
    assert captured[0]["follow_up_note"]=="Call tomorrow"

def test_unknown_recording_scheme_not_persisted():
    result=sanitize({"id":"call_456","artifact":{"recordingUrl":"javascript:alert(1)"}},
        "00000000-0000-0000-0000-000000000001")
    assert result["recording_url"] is None


def test_custom_range_filters_tenant_and_calculates_caller_metrics(monkeypatch):
    from app.account import current_user

    queries = []
    rows = [
        {"caller_phone": "+12025550101", "duration_seconds": 120, "lead_stage": "hot"},
        {"caller_phone": "+12025550101", "duration_seconds": 60, "lead_stage": "general"},
        {"caller_phone": None, "duration_seconds": None, "lead_stage": "needs_review"},
    ]
    for row in rows:
        row.update(started_at="2026-11-05T20:00:00Z", follow_up_needed=False)

    def database(table, **kwargs):
        queries.append((table, kwargs["params"]))
        return rows

    monkeypatch.setattr(ci, "membership", lambda oid, user: None)
    monkeypatch.setattr(ci, "api", database)
    app.dependency_overrides[current_user] = lambda: {"id": "owner"}
    try:
        path = "/api/account/organizations/example-tenant/calls"
        dates = {"start": "2026-11-05T00:00:00-05:00", "end": "2026-11-06T00:00:00-05:00"}
        result = client.get(path, params={**dates, "search": "hot"}).json()
        assert result["total"] == 3
        assert result["unique_callers"] == 1
        assert result["average_duration_seconds"] == 90
        assert result["calls"] == rows[:1]
        assert not result["truncated"]
        assert queries[0][1]["organization_id"] == "eq.example-tenant"
        assert queries[0][1]["and"] == (
            "(started_at.gte.2026-11-05T00:00:00-05:00,started_at.lt.2026-11-06T00:00:00-05:00)"
        )

        for invalid in (
            {"start": dates["start"]},
            {"start": dates["end"], "end": dates["start"]},
            {"start": "2026-11-05T00:00:00", "end": dates["end"]},
        ):
            assert client.get(path, params=invalid).status_code == 422
        assert len(queries) == 1
    finally:
        app.dependency_overrides.pop(current_user, None)


def test_call_metrics_report_when_records_are_truncated(monkeypatch):
    row = {"caller_phone": None, "duration_seconds": None, "lead_stage": "general",
           "started_at": None, "follow_up_needed": False}
    monkeypatch.setattr(ci, "membership", lambda oid, user: None)
    monkeypatch.setattr(ci, "api", lambda *args, **kwargs: [row] * 301)
    result = ci.calls("example-tenant", days=30, user={"id": "owner"})
    assert result["truncated"] and result["total"] == 300
    assert result["unique_callers"] == 0 and result["average_duration_seconds"] is None
