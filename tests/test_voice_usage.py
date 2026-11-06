"""No billing without actual provider usage; no hidden paid provisioning."""
from datetime import datetime,timezone
from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app import usage

client=TestClient(app)
OID="00000000-0000-0000-0000-000000000001"

def test_usage_endpoint_requires_authentication():
    assert client.get(f"/api/account/organizations/{OID}/usage").status_code==401
    assert client.get(f"/api/account/organizations/{OID}/phone").status_code==401
    assert client.post(f"/api/account/organizations/{OID}/phone/link",
                       json={"provider_phone_id":"test_phone_id"}).status_code==401

def test_monthly_plan_usage_is_actual_minutes_and_warning(monkeypatch):
    monkeypatch.setattr(usage,"account",lambda oid:{
        "selected_plan":"starter","status":"active","enforcement_enabled":False,
        "current_period_end":"2026-12-01T00:00:00Z"})
    def api_stub(table,method="GET",data=None,**kwargs):
        assert table=="rpc/callpilot_usage_summary"
        return [{"call_count":50,"used_seconds":5400,"live_seconds":0}]
    monkeypatch.setattr(usage,"api",api_stub)
    result=usage.compute_usage(OID)
    assert result["minutes_included"]==100
    assert result["estimated_calls_at_2min"]==50
    assert result["minutes_used"]==90
    assert result["minutes_remaining"]==10
    assert result["alert"]=="warning"
    assert result["usage_status"]=="not_enforced"

def test_usage_exhausted_alert(monkeypatch):
    monkeypatch.setattr(usage,"account",lambda oid:{
        "selected_plan":"growth","status":"active","enforcement_enabled":True})
    monkeypatch.setattr(usage,"api",lambda table,method="GET",**kwargs:[
        {"call_count":10,"used_seconds":18000,"live_seconds":0}])
    outcome=usage.compute_usage(OID)
    assert outcome["minutes_remaining"]==0
    assert outcome["alert"]=="exhausted"
    assert outcome["enforcement_enabled"]

def test_no_plan_has_no_fake_allowance(monkeypatch):
    monkeypatch.setattr(usage,"account",lambda oid:{"status":"unconfigured"})
    monkeypatch.setattr(usage,"api",lambda table,**kwargs:[
        {"call_count":1,"used_seconds":131,"live_seconds":0}])
    x=usage.compute_usage(OID)
    assert x["calls_completed"]==1
    assert x["minutes_used"]==round(131/60,2)
    assert x["minutes_included"] is None
    assert x["minutes_remaining"] is None
    assert x["usage_status"]=="not_enforced"

def test_calendar_month_rollover():
    start,end=usage.billing_window({},datetime(2026,12,19,tzinfo=timezone.utc))
    assert start.isoformat().startswith("2026-12-01")
    assert end.isoformat().startswith("2027-01-01")

def test_inbound_admission_requires_provider_authorization():
    response=client.post(f"/webhooks/vapi-admission/{OID}",json={
        "message":{"type":"assistant-request","phoneNumber":{"id":"test"}}})
    assert response.status_code==401

def test_enabling_live_enforcement_is_blocked_in_sandbox(monkeypatch):
    monkeypatch.setenv("STRIPE_MODE","test")
    from app.account import current_user
    app.dependency_overrides[current_user]=lambda:{"id":"a","email":"test@example.com"}
    monkeypatch.setattr(usage,"membership",lambda *args,**kwargs:"owner")
    try:
        response=client.post(f"/api/account/organizations/{OID}/phone/activate-limits",
             json={"confirm":True},headers={"Authorization":"Bearer testing"})
        assert response.status_code==409
        assert "sandbox" in response.json()["detail"]
    finally:
        app.dependency_overrides.clear()
