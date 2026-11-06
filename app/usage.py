"""CallPilot monthly voice allowances and business phone-number management.

All usage originates from real completed Vapi calls. No invented call counts.
Provider admission is opt-in per business after an inbound-routing test.
"""
from __future__ import annotations
import os, hmac, calendar, re
from datetime import datetime,timezone,timedelta
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from app.account import current_user, membership
from app.cloud import api
from app.billing import PLANS, account
from app.call_intel import vapi, agent_config

router=APIRouter(tags=["voice-usage"])

def billing_window(row,now=None):
    now=now or datetime.now(timezone.utc)
    stop_text=(row or {}).get("current_period_end")
    if stop_text:
        try:
            stop=datetime.fromisoformat(stop_text.replace("Z","+00:00"))
            if stop.tzinfo is None: stop=stop.replace(tzinfo=timezone.utc)
            if stop>now:
                year=stop.year-(1 if stop.month==1 else 0)
                month=12 if stop.month==1 else stop.month-1
                start=stop.replace(year=year,month=month,
                                   day=min(stop.day,calendar.monthrange(year,month)[1]))
                if start<=now:return start,stop
        except (ValueError,TypeError):pass
    # If no active subscription cycle is recorded, show this month's illustrative usage.
    start=now.replace(day=1,hour=0,minute=0,second=0,microsecond=0)
    year=start.year+(start.month==12)
    month=1 if start.month==12 else start.month+1
    return start,start.replace(year=year,month=month)

def compute_usage(oid):
    a=account(oid) or {}
    plan=(a.get("selected_plan") or "").lower()
    if plan not in PLANS:plan=None
    start,end=billing_window(a)
    result=api("rpc/callpilot_usage_summary","POST",data={
        "org":oid,"since":start.isoformat(),"until":end.isoformat()})
    row=result[0] if isinstance(result,list) and result else {}
    seconds=int(row.get("used_seconds") or 0)
    active_seconds=int(row.get("live_seconds") or 0)
    calls=int(row.get("call_count") or 0)
    max_seconds=PLANS[plan]["included_minutes"]*60 if plan else None
    percentage=round(100*seconds/max_seconds,1) if max_seconds else None
    warning="none"
    if percentage is not None:
        if percentage>=100:warning="exhausted"
        elif percentage>=95:warning="critical"
        elif percentage>=80:warning="warning"
    eligible=bool(a.get("status") in ("active","trialing") and
                  a.get("enforcement_enabled") and max_seconds)
    return {
        "plan":plan,"subscription_status":a.get("status") or "not_configured",
        "period_start":start.isoformat(),"period_end":end.isoformat(),
        "minutes_included":PLANS[plan]["included_minutes"] if plan else None,
        "calls_completed":calls,"estimated_calls_at_2min":(PLANS[plan]["included_minutes"]//2 if plan else None),
        "seconds_used":seconds,"minutes_used":round(seconds/60,2),
        "seconds_active":active_seconds,
        "minutes_remaining":round(max(0,(max_seconds or 0)-seconds)/60,2) if max_seconds is not None else None,
        "usage_percent":percentage,
        "alert":warning,"enforcement_enabled":eligible,
        "usage_status":"enforced" if eligible else "not_enforced",
        "scope":"Inbound connected AI calls. Overage billing is disabled.",
        "note":"Estimated call counts assume two minutes per call. Actual allowance is measured in minutes."
    }

def record_threshold_alerts(oid:str):
    """Write each 80/95/100 percent alert once per business billing cycle."""
    usage=compute_usage(oid)
    if usage["minutes_included"] is None:
        return {"created":0}
    crossed=[level for level in (80,95,100)
             if usage["usage_percent"] is not None and usage["usage_percent"]>=level]
    for level in crossed:
        api("usage_alerts","POST",params={
            "on_conflict":"organization_id,period_start,threshold"},
            data={"organization_id":oid,"period_start":usage["period_start"],
                  "threshold":level},
            prefer="resolution=ignore-duplicates,return=minimal")
    return {"created":len(crossed)}

@router.get("/api/account/organizations/{oid}/usage/alerts")
def get_usage_alerts(oid:str,user=Depends(current_user)):
    membership(oid,user)
    usage=compute_usage(oid)
    rows=api("usage_alerts",params={
        "organization_id":"eq."+oid,
        "period_start":"eq."+usage["period_start"],
        "select":"threshold,created_at","order":"threshold.desc","limit":"3"})
    return {"alerts":rows,"period_start":usage["period_start"],
            "delivery":"dashboard","external_email_enabled":False}

@router.get("/api/account/organizations/{oid}/usage")
def usage(oid:str,user=Depends(current_user)):
    membership(oid,user)
    return compute_usage(oid)

@router.get("/api/account/organizations/{oid}/phone")
def business_phone(oid:str,user=Depends(current_user)):
    membership(oid,user)
    config=agent_config(oid)
    number_id=config.get("provider_phone_id")
    if not number_id:
        return {"connected":False,"provider":"vapi","phone_number":None,
                "number_id":None,"provisioning":"not_connected"}
    rows=api("phone_assignments",params={"organization_id":"eq."+oid,
        "provider_phone_id":"eq."+number_id,"select":"provider_number,status","limit":1})
    detail=api("phone_assignments",params={"organization_id":"eq."+oid,
        "provider_phone_id":"eq."+number_id,
        "select":"fallback_enabled,fallback_number","limit":1})
    config=detail[0] if detail else {}
    return {"connected":True,"provider":"vapi","number_id":number_id,
        "phone_number":rows[0].get("provider_number") if rows else None,
        "fallback_enabled":bool(config.get("fallback_enabled")),
        "fallback_number":config.get("fallback_number"),
        "provisioning":"linked"}

@router.get("/api/account/organizations/{oid}/phone/options")
def available_business_numbers(oid:str,user=Depends(current_user)):
    """Only show Vapi numbers already attached to this business's assistant."""
    membership(oid,user,("owner","admin"))
    assistant=agent_config(oid).get("provider_agent_id")
    if not assistant:return {"numbers":[],"message":"Connect your Vapi assistant first"}
    results=vapi("GET","/phone-number",params={"limit":100})
    if isinstance(results,dict):
        results=results.get("data") or results.get("results") or []
    if not isinstance(results,list):results=[]
    owned=api("phone_assignments",params={"select":"organization_id,provider_phone_id","limit":200})
    claimed={row["provider_phone_id"]:row["organization_id"] for row in owned}
    choices=[{"id":item["id"],"number":item.get("number") or "Number pending activation"}
        for item in results if isinstance(item,dict) and item.get("assistantId")==assistant
        and isinstance(item.get("id"),str)
        and claimed.get(item["id"],oid)==oid]
    return {"numbers":choices,"message":"Only numbers currently assigned to your business's assistant are shown."}

class PhoneLink(BaseModel):
    provider_phone_id:str=Field(min_length=5,max_length=150)

@router.post("/api/account/organizations/{oid}/phone/link")
def link_phone(oid:str,body:PhoneLink,user=Depends(current_user)):
    membership(oid,user,("owner","admin"))
    agent=agent_config(oid).get("provider_agent_id")
    if not agent:raise HTTPException(409,"Connect the business's Vapi assistant first")
    phone=vapi("GET","/phone-number/"+body.provider_phone_id)
    if phone.get("id")!=body.provider_phone_id:
        raise HTTPException(404,"Vapi number not found")
    if phone.get("assistantId")!=agent:
        raise HTTPException(409,"This number is not assigned to this business's Vapi assistant")
    assigned=api("phone_assignments",params={"provider_phone_id":"eq."+body.provider_phone_id,
       "select":"organization_id","limit":5})
    if any(row["organization_id"]!=oid for row in assigned):
        raise HTTPException(409,"That phone number is already assigned to another business")
    number=phone.get("number") if isinstance(phone.get("number"),str) else None
    api("phone_assignments","POST",params={"on_conflict":"provider_phone_id"},
        data={"organization_id":oid,"provider_phone_id":body.provider_phone_id,
              "provider_number":number,"status":"linked"},
        prefer="resolution=merge-duplicates,return=minimal")
    api("agent_configs","PATCH",params={"organization_id":"eq."+oid},
        data={"provider_phone_id":body.provider_phone_id},prefer="return=minimal")
    return {"linked":True,"number":number,"number_id":body.provider_phone_id,
            "admission_control":"not_enabled_on_phone"}

@router.post("/webhooks/vapi-admission/{oid}")
async def inbound_admission(oid:str,request:Request,authorization:str|None=Header(default=None)):
    token=os.getenv("VAPI_WEBHOOK_TOKEN","")
    if not token or not authorization or not hmac.compare_digest(authorization,"Bearer "+token):
        raise HTTPException(401,"Invalid provider credential")
    raw=await request.body()
    if len(raw)>16384:raise HTTPException(413,"Payload too large")
    try:
        import json
        message=json.loads(raw).get("message",{})
    except (ValueError,TypeError):
        raise HTTPException(400,"Invalid provider payload")
    if message.get("type")!="assistant-request":
        return {"error":"Unsupported call routing request"}
    config=agent_config(oid)
    phone_id=config.get("provider_phone_id")
    provided=(message.get("phoneNumber") or {}).get("id") or (message.get("call") or {}).get("phoneNumberId")
    if not phone_id or provided!=phone_id:
        return {"error":"This number is not configured for this business."}
    assigned=api("phone_assignments",params={"provider_phone_id":"eq."+phone_id,
        "organization_id":"eq."+oid,"select":"id","limit":1})
    if not assigned:return {"error":"This business line is unavailable."}
    agent_id=config.get("provider_agent_id")
    if not agent_id:return {"error":"This business line is not ready."}
    try:
        usage=compute_usage(oid)
    except Exception:
        # For production admission, fail closed on temporary billing data failures
        # rather than allowing unmetered calls.
        return {"error":"This line is temporarily unavailable. Please call again later."}
    if usage["usage_status"]!="enforced":
        # Only configure this endpoint on a phone after paying account,
        # verified plan and enforcement have been enabled.
        return {"error":"This business line has not been activated yet."}
    # Count minutes from ongoing calls as well as completed calls at admission.
    # A live call is never interrupted; another new call may be rejected.
    if (usage["minutes_remaining"]<=0 or
        usage["seconds_used"]+usage["seconds_active"]>=usage["minutes_included"]*60 or
        usage["subscription_status"] not in ("active","trialing")):
        fallback=api("phone_assignments",params={"organization_id":"eq."+oid,
            "provider_phone_id":"eq."+phone_id,
            "select":"fallback_enabled,fallback_number","limit":1})
        config=fallback[0] if fallback else {}
        # The owner must explicitly opt in to carrier-billed human forwarding.
        if config.get("fallback_enabled") and config.get("fallback_number"):
            return {"destination":{"type":"number","number":config["fallback_number"],
                    "message":"Connecting you to the business team."}}
        return {"error":"The AI receptionist is currently unavailable. Please contact the business directly or try again later."}
    return {"assistantId":agent_id}

class HumanFallback(BaseModel):
    number:str|None=None
    enabled:bool=False

@router.put("/api/account/organizations/{oid}/phone/fallback")
def configure_fallback(oid:str,body:HumanFallback,user=Depends(current_user)):
    """Optional human transfer. Never enabled without explicit owner action."""
    membership(oid,user,("owner",))
    import re
    number=(body.number or "").strip() or None
    if body.enabled and (not number or not re.fullmatch(r"\+[1-9]\d{7,14}",number)):
        raise HTTPException(422,"Enter a valid international-format phone number, such as +12125551234")
    cfg=agent_config(oid)
    pid=cfg.get("provider_phone_id")
    if not pid:raise HTTPException(409,"Link a business phone number first")
    rows=api("phone_assignments",params={"organization_id":"eq."+oid,
        "provider_phone_id":"eq."+pid,"select":"id"})
    if not rows:raise HTTPException(409,"Business number not verified")
    api("phone_assignments","PATCH",params={"organization_id":"eq."+oid,
        "provider_phone_id":"eq."+pid},
        data={"fallback_enabled":body.enabled,"fallback_number":number},
        prefer="return=minimal")
    return {"saved":True,"fallback_enabled":body.enabled,
        "warning":"Forwarded calls may incur telephony charges even when AI minutes are exhausted."}

class ActivateRequest(BaseModel):
    confirm:bool=False

@router.post("/api/account/organizations/{oid}/phone/activate-limits")
def activate_limits(oid:str,body:ActivateRequest,user=Depends(current_user)):
    """High-impact routing operation gated on real billing and owner confirmation.

    This endpoint deliberately does NOT activate on sandbox subscriptions.
    """
    membership(oid,user,("owner",))
    if not body.confirm:
        raise HTTPException(400,"Confirm activation of inbound call limits first")
    if os.getenv("STRIPE_MODE")!="live":
        raise HTTPException(409,"Production enforcement is not available during sandbox billing")
    billing=account(oid) or {}
    if (billing.get("status") not in ("active","trialing") or
        billing.get("selected_plan") not in PLANS or
        not billing.get("setup_paid_at")):
        raise HTTPException(409,"A fully paid subscription and setup fee are required")
    if not os.getenv("VAPI_WEBHOOK_TOKEN"):
        raise HTTPException(503,"Secure Vapi webhook token is not configured")
    credential=os.getenv("VAPI_CREDENTIAL_ID","")
    if not credential:
        raise HTTPException(503,"Secure Vapi routing credential is not configured")
    config=agent_config(oid)
    phone_id=config.get("provider_phone_id")
    agent_id=config.get("provider_agent_id")
    if not phone_id or not agent_id:
        raise HTTPException(409,"Connect a Vapi phone number and assistant first")
    rows=api("phone_assignments",params={
        "organization_id":"eq."+oid,"provider_phone_id":"eq."+phone_id,
        "select":"provider_phone_id","limit":1})
    if not rows:raise HTTPException(409,"Phone number ownership not verified")
    provider=vapi("GET","/phone-number/"+phone_id)
    if provider.get("assistantId")!=agent_id:
        raise HTTPException(409,"Provider number routing differs from this workspace")
    base=os.getenv("PUBLIC_BASE_URL","https://callpilot-internal.onrender.com").rstrip("/")
    if not base.startswith("https://"):
        raise HTTPException(503,"Secure HTTPS application URL required")
    endpoint=base+"/webhooks/vapi-admission/"+oid
    # On webhook timeout/error, route to a human only if the owner explicitly
    # enabled it. Otherwise play a brief provider announcement rather than
    # consuming more billable AI minutes.
    settings=api("phone_assignments",params={
        "organization_id":"eq."+oid,"provider_phone_id":"eq."+phone_id,
        "select":"fallback_enabled,fallback_number","limit":1})
    fallback=settings[0] if settings else {}
    if fallback.get("fallback_enabled") and fallback.get("fallback_number"):
        failure_action={"type":"transfer","destination":{
            "type":"number","number":fallback["fallback_number"]}}
    else:
        failure_action={"type":"say","exact":
            "The AI receptionist is unavailable right now. Please try again later."}
    hooks=[h for h in (provider.get("hooks") or [])
        if not (isinstance(h,dict) and h.get("name")=="callpilot-assistant-failure")]
    hooks.append({"name":"callpilot-assistant-failure","on":"call.ending",
        "filters":[{"type":"oneOf","key":"call.endedReason",
          "oneOf":["assistant-request-failed","assistant-request-returned-error",
                   "assistant-request-returned-no-assistant"]}],
        "do":failure_action})
    vapi("PATCH","/phone-number/"+phone_id,body={
        "assistantId":None,"hooks":hooks,
        "server":{"url":endpoint,"credentialId":credential,"timeoutSeconds":6}})
    try:
        api("billing_accounts","PATCH",params={"organization_id":"eq."+oid},
            data={"enforcement_enabled":True},prefer="return=minimal")
    except Exception:
        try:vapi("PATCH","/phone-number/"+phone_id,body={"assistantId":agent_id})
        except Exception:pass
        raise HTTPException(503,"Could not save call limits; number routing rollback attempted")
    return {"enforced":True,"note":"Future calls must pass monthly usage admission; ongoing calls are not interrupted"}

@router.get("/api/account/organizations/{oid}/phone/setup-guide")
def phone_setup_guide(oid:str,user=Depends(current_user)):
    membership(oid,user,("owner","admin"))
    return {
        "steps":["Choose a U.S. business number with a supported carrier such as Twilio, or forward an existing number.",
                 "Import the number into Vapi and assign the business's AI assistant.",
                 "Connect the provider phone-number ID to this workspace.",
                 "Test inbound calls before enabling billing-enforced routing."],
        "note":"CallPilot does not buy numbers or activate enforcement without a tested configuration.",
        "free_vapi_numbers_per_basic_account":1,
        "paid_numbers":"Additional numbers have provider charges; usage/concurrency is separate."
    }
