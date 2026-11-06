"""Release-critical safeguards: Stripe mode, quotas and Vapi duration policy."""
import pytest
from fastapi import HTTPException
from app import billing, call_intel, usage

def test_stripe_disabled_without_explicit_approval(monkeypatch):
    monkeypatch.setenv("STRIPE_MODE","live")
    monkeypatch.setenv("STRIPE_BILLING_ENABLED","true")
    monkeypatch.setenv("STRIPE_SECRET_KEY","sk_live_only_for_test")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET","whsec_only_for_test")
    monkeypatch.delenv("STRIPE_LIVE_APPROVED",raising=False)
    assert billing.billing_ready() is False

def test_stripe_live_requires_preapproved_prices(monkeypatch):
    monkeypatch.setenv("STRIPE_MODE","live")
    monkeypatch.setenv("STRIPE_BILLING_ENABLED","true")
    monkeypatch.setenv("STRIPE_SECRET_KEY","sk_live_only_for_test")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET","whsec_only_for_test")
    monkeypatch.setenv("STRIPE_LIVE_APPROVED","true")
    for k in ("STRIPE_PRICE_STARTER","STRIPE_PRICE_GROWTH",
              "STRIPE_PRICE_BUSINESS","STRIPE_PRICE_SETUP"):
        monkeypatch.delenv(k,raising=False)
    assert billing.billing_ready() is False

def test_stripe_webhook_signature_required():
    assert not billing.check_signature(b'{}',"", "")
    assert not billing.check_signature(b'{}',"t=0,v1=bad","whsec_test")

def test_four_minute_vapi_policy_is_idempotent(monkeypatch):
    monkeypatch.setattr(call_intel,"agent_config",lambda oid:{"provider_agent_id":"asst_test"})
    calls=[]
    assistant={"id":"asst_test","maxDurationSeconds":600,"hooks":[]}
    def fake_vapi(method,path,**kwargs):
        calls.append((method,path,kwargs.get("body")))
        if method=="PATCH":
            assistant.update(kwargs["body"])
        return dict(assistant)
    monkeypatch.setattr(call_intel,"vapi",fake_vapi)
    first=call_intel.apply_call_duration_policy("org_test")
    second=call_intel.apply_call_duration_policy("org_test")
    assert first["configured"] and first["changed"]
    assert not second["changed"]
    assert assistant["maxDurationSeconds"]==240
    hook=assistant["hooks"][-1]
    assert hook["options"]["seconds"]==215

def test_usage_requires_subscription_billing_cycle(monkeypatch):
    monkeypatch.setattr(usage,"account",lambda oid:{
        "selected_plan":"starter","status":"active",
        "enforcement_enabled":True,"current_period_end":None})
    monkeypatch.setattr(usage,"api",lambda *args,**kwargs:[{
        "used_seconds":0,"live_seconds":0,"call_count":0}])
    x=usage.compute_usage("org_test")
    assert x["usage_status"]=="not_enforced"

def test_zero_minutes_stays_zero(monkeypatch):
    monkeypatch.setattr(usage,"account",lambda oid:{"selected_plan":"starter","status":"active"})
    monkeypatch.setattr(usage,"api",lambda *args,**kwargs:[{
        "used_seconds":6000,"live_seconds":0,"call_count":35}])
    x=usage.compute_usage("org_test")
    assert x["minutes_remaining"]==0
    assert x["alert"]=="exhausted"
