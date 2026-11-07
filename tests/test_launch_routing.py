"""Tenant isolation, monthly minute limits, admission/forwarding and the four-minute cap."""
from datetime import datetime, timezone, timedelta
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from app.main import app
from app import usage, vapi, call_intel, cloud
from app.account import current_user

client=TestClient(app)
A="00000000-0000-0000-0000-00000000000a"
B="00000000-0000-0000-0000-00000000000b"
TOKEN="unit-test-vapi-token"
AUTH={"Authorization":"Bearer "+TOKEN}

@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("VAPI_WEBHOOK_TOKEN",TOKEN)
    monkeypatch.setattr(vapi,"org_or_404",lambda oid:{"id":oid})
    yield
    app.dependency_overrides.clear()

# ---------- tenant isolation ----------
def agent_rows(mapping):
    def api(table,method="GET",params=None,**kw):
        if table=="agent_configs":
            oid=(params or {}).get("organization_id","")[3:]
            return [{"provider_agent_id":mapping.get(oid),"provider_phone_id":"ph_"+oid[-1]}]
        return []
    return api

def test_tool_webhook_rejects_a_call_from_another_workspaces_assistant(monkeypatch):
    monkeypatch.setattr(vapi,"api",agent_rows({A:"asst_A",B:"asst_B"}))
    body={"message":{"type":"tool-calls","call":{"assistantId":"asst_B"},"toolCallList":[]}}
    assert client.post(f"/webhooks/vapi-tools/{A}",json=body,headers=AUTH).status_code==403
    ok={"message":{"type":"tool-calls","call":{"assistantId":"asst_A"},"toolCallList":[]}}
    assert client.post(f"/webhooks/vapi-tools/{A}",json=ok,headers=AUTH).status_code==200

def test_end_of_call_report_cannot_be_filed_under_another_workspace(monkeypatch):
    stored=[]
    def api(table,method="GET",params=None,data=None,**kw):
        if table=="agent_configs":return [{"provider_agent_id":"asst_A","provider_phone_id":"ph"}]
        stored.append((table,method));return []
    monkeypatch.setattr(vapi,"api",api)
    body={"message":{"type":"end-of-call-report","call":{"id":"call_1","assistantId":"asst_B"}}}
    assert client.post(f"/webhooks/vapi/{A}",json=body,headers=AUTH).status_code==403
    assert stored==[]

def test_webhooks_reject_missing_or_wrong_token():
    body={"message":{"type":"tool-calls","toolCallList":[]}}
    assert client.post(f"/webhooks/vapi-tools/{A}",json=body).status_code==401
    assert client.post(f"/webhooks/vapi-tools/{A}",json=body,headers={"Authorization":"Bearer nope"}).status_code==401
    assert client.post(f"/webhooks/vapi-admission/{A}",json={}).status_code==401

def test_assistant_ownership_is_a_tag_not_a_display_name():
    assert call_intel.owns_assistant({"metadata":{"callpilot_org":A},"name":"x"},A)
    assert not call_intel.owns_assistant({"metadata":{"callpilot_org":B},"name":"Biz - CallPilot"},A)
    assert not call_intel.owns_assistant({"name":"Biz - CallPilot"},A)

def test_linking_requires_ownership_tag_even_if_the_name_matches(monkeypatch):
    app.dependency_overrides[current_user]=lambda:{"id":"u"}
    monkeypatch.setattr(call_intel,"membership",lambda *a,**k:"owner")
    monkeypatch.setattr(call_intel,"org_or_404",lambda oid:{"name":"Biz"})
    monkeypatch.setattr(call_intel,"vapi",lambda *a,**k:{"id":"asst_X","name":"Biz - CallPilot lookalike"})
    r=client.post(f"/api/account/organizations/{A}/voice-assistants/link",json={"assistant_id":"asst_X"})
    assert r.status_code==403

def test_assistant_already_linked_elsewhere_cannot_be_claimed(monkeypatch):
    app.dependency_overrides[current_user]=lambda:{"id":"u"}
    monkeypatch.setattr(call_intel,"membership",lambda *a,**k:"owner")
    monkeypatch.setattr(call_intel,"vapi",lambda *a,**k:{"id":"asst_X","metadata":{"callpilot_org":A}})
    monkeypatch.setattr(call_intel,"api",lambda *a,**k:[{"organization_id":B}])
    r=client.post(f"/api/account/organizations/{A}/voice-assistants/link",json={"assistant_id":"asst_X"})
    assert r.status_code==409

# ---------- four-minute cap ----------
def test_provisioned_assistant_is_capped_at_240_seconds_with_wrapup(monkeypatch):
    sent={}
    class FakeResp:
        status_code=201;text="";
        def json(self):return {"id":"asst_new"}
    class FakeClient:
        def __init__(self,*a,**k):pass
        def __enter__(self):return self
        def __exit__(self,*a):return False
        def post(self,url,headers=None,json=None):sent["payload"]=json;return FakeResp()
    def api(table,method="GET",params=None,**kw):
        if table=="agent_configs" and method=="GET":
            return [{"agent_name":"Alex","greeting":"Hello, I am the AI assistant.","voice_provider":"standard",
                     "voice_id":"","voice_consent_verified_at":None,"provider_agent_id":None}]
        return []
    monkeypatch.setattr(cloud.httpx,"Client",FakeClient)
    monkeypatch.setattr(cloud,"api",api)
    monkeypatch.setattr(cloud,"org_or_404",lambda oid:{"id":oid,"name":"United Global Trade LLC"})
    monkeypatch.setenv("VAPI_API_KEY","k");monkeypatch.setenv("VAPI_CREDENTIAL_ID","cred_1")
    monkeypatch.setattr(cloud,"authorize",lambda:None)
    out=cloud.provision_test_agent(A)
    p=sent["payload"]
    assert out["created"] and p["maxDurationSeconds"]==240
    assert p["hooks"][0]["options"]["seconds"]==215
    assert p["metadata"]=={"callpilot_org":A}
    assert len(p["name"])<=40 and p["voice"]["provider"]=="vapi"

def test_duration_policy_patches_cap_keeps_other_hooks_and_tags_owner(monkeypatch):
    calls=[]
    state={"id":"asst_A","hooks":[{"name":"other-hook"}],"maxDurationSeconds":600}
    def vapi_stub(method,path,body=None,params=None):
        calls.append((method,body))
        if method=="PATCH":state.update(body)
        return dict(state)
    monkeypatch.setattr(call_intel,"vapi",vapi_stub)
    monkeypatch.setattr(call_intel,"agent_config",lambda oid:{"provider_agent_id":"asst_A"})
    out=call_intel.apply_call_duration_policy(A)
    assert out["configured"] and state["maxDurationSeconds"]==240
    assert {h["name"] for h in state["hooks"]}=={"other-hook","callpilot-four-minute-wrapup"}
    assert state["metadata"]["callpilot_org"]==A
    assert call_intel.apply_call_duration_policy(A)["changed"] is False

def test_duration_policy_refuses_an_assistant_tagged_to_another_workspace(monkeypatch):
    monkeypatch.setattr(call_intel,"vapi",lambda *a,**k:{"id":"asst_A","metadata":{"callpilot_org":B}})
    monkeypatch.setattr(call_intel,"agent_config",lambda oid:{"provider_agent_id":"asst_A"})
    with pytest.raises(HTTPException) as e:call_intel.apply_call_duration_policy(A)
    assert e.value.status_code==409

# ---------- monthly limits ----------
def now_window():
    now=datetime.now(timezone.utc)
    start=now.replace(day=1,hour=0,minute=0,second=0,microsecond=0)
    return start,start+timedelta(days=31)

def usage_for(monkeypatch,plan,used_seconds,live=0,status="active"):
    start,end=now_window()
    monkeypatch.setattr(usage,"account",lambda oid:{"selected_plan":plan,"status":status,
        "enforcement_enabled":True,"current_period_start":start.isoformat(),"current_period_end":end.isoformat()})
    seen={}
    def api(table,method="GET",data=None,**kw):
        seen["data"]=data
        return [{"call_count":3,"used_seconds":used_seconds,"live_seconds":live}]
    monkeypatch.setattr(usage,"api",api)
    return usage.compute_usage(A),seen

@pytest.mark.parametrize("plan,minutes",[("starter",100),("growth",300),("business",600)])
def test_each_plan_enforces_its_published_minutes(monkeypatch,plan,minutes):
    r,_=usage_for(monkeypatch,plan,0)
    assert r["minutes_included"]==minutes and r["usage_status"]=="enforced"

@pytest.mark.parametrize("fraction,alert",[(0.79,"none"),(0.80,"warning"),(0.95,"critical"),(1.0,"exhausted")])
def test_warning_thresholds_at_80_95_100_percent(monkeypatch,fraction,alert):
    r,_=usage_for(monkeypatch,"starter",int(100*60*fraction))
    assert r["alert"]==alert

def test_usage_uses_actual_provider_seconds_and_stripe_cycle_window(monkeypatch):
    start,end=now_window()
    r,seen=usage_for(monkeypatch,"starter",131)
    assert r["seconds_used"]==131 and r["minutes_used"]==round(131/60,2)
    assert seen["data"]["since"]==start.isoformat() and seen["data"]["until"]==end.isoformat()

def test_new_stripe_cycle_resets_the_window():
    now=datetime(2026,12,20,tzinfo=timezone.utc)
    s,e=usage.billing_window({"current_period_start":"2026-12-15T00:00:00Z","current_period_end":"2027-01-15T00:00:00Z"},now)
    assert s.isoformat().startswith("2026-12-15") and e.isoformat().startswith("2027-01-15")
    s2,e2=usage.billing_window({"current_period_start":"2027-01-15T00:00:00Z","current_period_end":"2027-02-15T00:00:00Z"},
                               datetime(2027,1,16,tzinfo=timezone.utc))
    assert s2.isoformat().startswith("2027-01-15")

def test_threshold_alerts_are_written_once_per_cycle_by_unique_key(monkeypatch):
    start,end=now_window()
    monkeypatch.setattr(usage,"compute_usage",lambda oid:{"minutes_included":100,"usage_percent":96.0,
        "period_start":start.isoformat()})
    writes=[]
    monkeypatch.setattr(usage,"api",lambda table,method="GET",params=None,data=None,prefer=None:writes.append((params,data,prefer)))
    assert usage.record_threshold_alerts(A)["created"]==2
    assert all(p["on_conflict"]=="organization_id,period_start,threshold" for p,_,_ in writes)
    assert all("ignore-duplicates" in pr for _,_,pr in writes)
    assert [d["threshold"] for _,d,_ in writes]==[80,95]

def test_call_records_are_idempotent_on_provider_call_id(monkeypatch):
    calls=[]
    monkeypatch.setattr(call_intel,"api",lambda table,method="GET",params=None,data=None,prefer=None:
        calls.append((table,method,params)) or [])
    call={"id":"call_1","type":"inboundPhoneCall","startedAt":"2026-11-06T06:10:00Z",
          "endedAt":"2026-11-06T06:12:11Z","artifact":{}}
    call_intel.persist(A,call);call_intel.persist(A,call)
    posts=[c for c in calls if c[1]=="POST"]
    assert len(posts)==2 and all(c[2]["on_conflict"]=="organization_id,provider_call_id" for c in posts)

# ---------- admission and human forwarding ----------
def admission(monkeypatch,remaining=10,used=0,active=0,included=100,status="active",
              fallback=None,enforced=True,phone="ph_1"):
    monkeypatch.setattr(usage,"agent_config",lambda oid:{"provider_phone_id":"ph_1","provider_agent_id":"asst_A"})
    def api(table,method="GET",params=None,**kw):
        if table=="phone_assignments":
            return [fallback or {"id":1}] if "fallback_enabled" in (params or {}).get("select","") else [{"id":1}]
        return []
    monkeypatch.setattr(usage,"api",api)
    monkeypatch.setattr(usage,"compute_usage",lambda oid:{"usage_status":"enforced" if enforced else "not_enforced",
        "minutes_remaining":remaining,"seconds_used":used,"seconds_active":active,
        "minutes_included":included,"subscription_status":status})
    body={"message":{"type":"assistant-request","phoneNumber":{"id":phone}}}
    return client.post(f"/webhooks/vapi-admission/{A}",json=body,headers=AUTH).json()

def test_admission_connects_the_workspaces_own_assistant_when_allowed(monkeypatch):
    assert admission(monkeypatch)=={"assistantId":"asst_A"}

def test_exhausted_allowance_declines_new_ai_calls_without_forwarding(monkeypatch):
    out=admission(monkeypatch,remaining=0,used=6000)
    assert "assistantId" not in out and "destination" not in out and "unavailable" in out["error"].lower()

def test_exhausted_allowance_forwards_only_when_owner_enabled_it(monkeypatch):
    out=admission(monkeypatch,remaining=0,used=6000,
                  fallback={"fallback_enabled":True,"fallback_number":"+12125551234"})
    assert out["destination"]["number"]=="+12125551234"
    off=admission(monkeypatch,remaining=0,used=6000,
                  fallback={"fallback_enabled":False,"fallback_number":"+12125551234"})
    assert "destination" not in off

def test_in_progress_calls_count_toward_admission_but_are_never_interrupted(monkeypatch):
    out=admission(monkeypatch,remaining=5,used=5700,active=300)
    assert "assistantId" not in out

def test_canceled_or_unpaid_subscription_declines_new_calls(monkeypatch):
    assert "assistantId" not in admission(monkeypatch,status="canceled")
    assert "assistantId" not in admission(monkeypatch,status="past_due")

def test_admission_is_inactive_until_enforcement_is_enabled(monkeypatch):
    assert "not been activated" in admission(monkeypatch,enforced=False)["error"]

def test_admission_rejects_a_number_that_is_not_this_workspaces(monkeypatch):
    assert "not configured" in admission(monkeypatch,phone="ph_other")["error"]

def test_admission_fails_closed_when_billing_data_is_unavailable(monkeypatch):
    admission(monkeypatch)
    def boom(oid):raise RuntimeError("db down")
    monkeypatch.setattr(usage,"compute_usage",boom)
    body={"message":{"type":"assistant-request","phoneNumber":{"id":"ph_1"}}}
    out=client.post(f"/webhooks/vapi-admission/{A}",json=body,headers=AUTH).json()
    assert "assistantId" not in out and "temporarily unavailable" in out["error"]

def test_forwarding_requires_explicit_valid_number_and_owner_role(monkeypatch):
    app.dependency_overrides[current_user]=lambda:{"id":"u"}
    monkeypatch.setattr(usage,"membership",lambda *a,**k:"owner")
    monkeypatch.setattr(usage,"agent_config",lambda oid:{"provider_phone_id":"ph_1"})
    saved=[]
    monkeypatch.setattr(usage,"api",lambda table,method="GET",params=None,data=None,prefer=None:
        saved.append(data) if method=="PATCH" else [{"id":1}])
    url=f"/api/account/organizations/{A}/phone/fallback"
    assert client.put(url,json={"enabled":True,"number":"555-1234"}).status_code==422
    assert client.put(url,json={"enabled":True}).status_code==422
    assert client.put(url,json={"enabled":True,"number":"+12125551234"}).status_code==200
    assert saved[-1]=={"fallback_enabled":True,"fallback_number":"+12125551234"}

def test_activation_of_call_blocking_requires_live_mode_and_confirmation(monkeypatch):
    app.dependency_overrides[current_user]=lambda:{"id":"u"}
    monkeypatch.setattr(usage,"membership",lambda *a,**k:"owner")
    url=f"/api/account/organizations/{A}/phone/activate-limits"
    assert client.post(url,json={"confirm":False}).status_code==400
    monkeypatch.setenv("STRIPE_MODE","test")
    assert client.post(url,json={"confirm":True}).status_code==409
