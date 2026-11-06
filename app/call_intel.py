"""Tenant-scoped business call intelligence and Vapi synchronization.

Only server-side Vapi/Supabase credentials are used. Never infer that a buyer
is verified from their statements or a model-generated score.
"""
from __future__ import annotations
import os, re, uuid
from datetime import datetime, timezone
from typing import Literal
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from app.account import current_user, membership
from app.cloud import api, org_or_404

router = APIRouter(prefix="/api/account/organizations/{oid}", tags=["call-intelligence"])
VALID_STAGES = {"hot","warm","general","not_qualified","needs_review"}
VALID_FOLLOW_UP = {"none","todo","in_progress","done"}

def vapi(method:str, path:str, *, params=None, body=None):
    key=os.getenv("VAPI_API_KEY","")
    if not key: raise HTTPException(503,"Vapi connection is not configured")
    try:
        with httpx.Client(timeout=22) as client:
            response=client.request(method,"https://api.vapi.ai"+path,
                headers={"Authorization":"Bearer "+key,"Content-Type":"application/json"},
                params=params,json=body)
    except httpx.RequestError:
        raise HTTPException(503,"Vapi is unavailable. Try refreshing later.")
    if response.status_code>=400:
        raise HTTPException(502,"Vapi request failed (HTTP "+str(response.status_code)+")")
    return response.json() if response.content else {}

def agent_config(oid:str):
    records=api("agent_configs",params={"organization_id":"eq."+oid,
        "select":"provider_agent_id,provider_phone_id"})
    return records[0] if records else {}

def as_text(value, maximum=2000):
    if not isinstance(value,str):return None
    value=value.strip()
    return value[:maximum] if value else None

def duration(call):
    a=call.get("startedAt") or call.get("createdAt")
    b=call.get("endedAt")
    if not a or not b:return None
    try:
        start=datetime.fromisoformat(a.replace("Z","+00:00"))
        end=datetime.fromisoformat(b.replace("Z","+00:00"))
        return max(0,int((end-start).total_seconds()))
    except (TypeError,ValueError):return None

def extracted(call:dict):
    """Vapi structured outputs are an object keyed by output ID; use result only."""
    artifact=call.get("artifact") or {}
    data=artifact.get("structuredOutputs") or {}
    if isinstance(data,dict):
        for output in data.values():
            result=output.get("result") if isinstance(output,dict) else None
            if isinstance(result,dict) and ("lead_stage" in result or "leadStage" in result):
                return result
    legacy=(call.get("analysis") or {}).get("structuredData")
    return legacy if isinstance(legacy,dict) else {}

def classify(data):
    stage=data.get("lead_stage") or data.get("leadStage")
    if stage not in VALID_STAGES:stage="needs_review"
    # Unverified and insufficient-evidence results stay in review.
    explanation=as_text(data.get("reason") or data.get("lead_reason"),600)
    if stage in ("hot","warm") and not explanation:stage="needs_review"
    return stage,explanation

def sanitize(call:dict,oid:str):
    if not isinstance(call,dict) or not isinstance(call.get("id"),str):
        raise HTTPException(422,"Invalid provider call record")
    artifact=call.get("artifact") or {}
    analysis=call.get("analysis") or {}
    structured=extracted(call)
    stage,reason=classify(structured)
    customer=call.get("customer") or {}
    recording=artifact.get("recordingUrl") or call.get("recordingUrl")
    if not isinstance(recording,str):
        item=artifact.get("recording") or {}
        recording=item.get("url") if isinstance(item,dict) else None
    transcript=artifact.get("transcript") or call.get("transcript")
    if not isinstance(transcript,str):
        transcript=None
    summary=analysis.get("summary")
    if not isinstance(summary,str):
        summary=structured.get("summary")
    return {
        "organization_id":oid,"provider_call_id":call["id"],"provider":"vapi",
        "assistant_id":as_text(call.get("assistantId"),150),
        "phone_number_id":as_text(call.get("phoneNumberId"),150),
        "direction":"inbound" if call.get("type")=="inboundPhoneCall" else "other",
        "caller_phone":as_text(customer.get("number"),40),
        "started_at":call.get("startedAt") or call.get("createdAt"),
        "ended_at":call.get("endedAt"),
        "duration_seconds":duration(call),
        "provider_status":as_text(call.get("status"),80),
        "ended_reason":as_text(call.get("endedReason"),180),
        "summary":as_text(summary,1800),
        "transcript":as_text(transcript,35000),
        "recording_url":as_text(recording,2200) if isinstance(recording,str) and recording.startswith("https://") else None,
        "lead_stage":stage,"lead_reason":reason,"lead_source":"ai_suggested" if structured else "unreviewed",
        "caller_name":as_text(structured.get("caller_name"),120),
        "product_interest":as_text(structured.get("product_interest"),200),
        "quantity_interest":as_text(structured.get("quantity_interest"),200),
        "destination":as_text(structured.get("destination"),200),
        "purchase_timeline":as_text(structured.get("purchase_timeline"),200),
        "follow_up_needed":structured.get("follow_up_needed") is True,
        "analyzed_at":datetime.now(timezone.utc).isoformat() if structured else None,
        "updated_at":datetime.now(timezone.utc).isoformat()
    }

def persist(oid:str,call:dict):
    data=sanitize(call,oid)
    existing=api("call_insights",params={"organization_id":"eq."+oid,
        "provider_call_id":"eq."+data["provider_call_id"],
        "select":"id,lead_source,lead_stage,lead_reason,follow_up_needed,follow_up_status,follow_up_note,follow_up_due_at,last_reviewed_by"})
    if existing:
        previous=existing[0]
        if previous["lead_source"]=="human_reviewed":
            for k in ("lead_source","lead_stage","lead_reason","follow_up_needed"):
                data[k]=previous[k]
        for k in ("follow_up_status","follow_up_note","follow_up_due_at","last_reviewed_by"):
            if previous.get(k) is not None:data[k]=previous[k]
    return api("call_insights","POST",params={"on_conflict":"organization_id,provider_call_id"},
        data=data,prefer="resolution=merge-duplicates,return=minimal")

@router.get("/calls")
def calls(oid:str,stage:str="all",search:str="",days:int=Query(30,ge=1,le=365),
          user=Depends(current_user)):
    membership(oid,user)
    rows=api("call_insights",params={"organization_id":"eq."+oid,
        "select":"id,provider_call_id,caller_phone,caller_name,started_at,duration_seconds,summary,lead_stage,lead_source,lead_reason,follow_up_needed,follow_up_status,product_interest,direction",
        "order":"started_at.desc.nullslast","limit":"300"})
    from datetime import timedelta
    cutoff=datetime.now(timezone.utc)-timedelta(days=days)
    filtered=[]
    for row in rows:
        stamp=row.get("started_at")
        try:
            if stamp and datetime.fromisoformat(stamp.replace("Z","+00:00"))<cutoff:continue
        except ValueError:continue
        if stage!="all" and row["lead_stage"]!=stage:continue
        q=search.casefold().strip()
        if q and not any(q in str(row.get(field) or "").casefold() for field in
                         ("caller_phone","caller_name","summary","product_interest")):continue
        filtered.append(row)
    # Counts are based on period (not the currently selected lead filter).
    period=[r for r in rows if (not r.get("started_at") or
        safe_after(r["started_at"],cutoff))]
    counts={k:sum(r["lead_stage"]==k for r in period) for k in VALID_STAGES}
    return {"calls":filtered,"total":len(period),"counts":counts,
            "follow_ups":sum(r["follow_up_needed"] and r["follow_up_status"]!="done" for r in period),
            "latest_sync":max((r["started_at"] for r in period if r.get("started_at")),default=None)}

def safe_after(stamp,cutoff):
    try:return datetime.fromisoformat(stamp.replace("Z","+00:00"))>=cutoff
    except (ValueError,TypeError):return False

@router.get("/calls/{call_id}")
def call_detail(oid:str,call_id:uuid.UUID,user=Depends(current_user)):
    membership(oid,user)
    rows=api("call_insights",params={"organization_id":"eq."+oid,"id":"eq."+str(call_id),
        "select":"*"})
    if not rows:raise HTTPException(404,"Call not found")
    return rows[0]

class Review(BaseModel):
    lead_stage:Literal["hot","warm","general","not_qualified","needs_review"]
    lead_reason:str=Field(default="",max_length=600)
    follow_up_needed:bool=False
    buyer_verified:bool=False
    follow_up_status:Literal["none","todo","in_progress","done"]="none"
    follow_up_note:str=Field(default="",max_length=1000)

@router.patch("/calls/{call_id}")
def review_call(oid:str,call_id:uuid.UUID,body:Review,user=Depends(current_user)):
    membership(oid,user,("owner","admin"))
    rows=api("call_insights",params={"organization_id":"eq."+oid,"id":"eq."+str(call_id),"select":"id"})
    if not rows:raise HTTPException(404,"Call not found")
    data=body.model_dump()
    data.update({"lead_source":"human_reviewed","last_reviewed_by":user["id"],
        "updated_at":datetime.now(timezone.utc).isoformat()})
    api("call_insights","PATCH",params={"organization_id":"eq."+oid,"id":"eq."+str(call_id)},
        data=data,prefer="return=minimal")
    return {"saved":True}

@router.get("/voice-assistants")
def voice_assistants(oid:str,user=Depends(current_user)):
    membership(oid,user,("owner","admin"))
    result=vapi("GET","/assistant",params={"limit":100})
    if not isinstance(result,list):result=[]
    business=org_or_404(oid)
    prefix=(business["name"]+" - CallPilot").casefold()
    return {"assistants":[{"id":x["id"],"name":x.get("name") or "Unnamed assistant"}
             for x in result if isinstance(x,dict) and isinstance(x.get("id"),str)
             and isinstance(x.get("name"),str) and x["name"].casefold().startswith(prefix)],
            "linked":agent_config(oid).get("provider_agent_id")}

class LinkAssistant(BaseModel):
    assistant_id:str=Field(min_length=5,max_length=150)

@router.post("/voice-assistants/link")
def link_voice_assistant(oid:str,body:LinkAssistant,user=Depends(current_user)):
    membership(oid,user,("owner","admin"))
    assistant=vapi("GET","/assistant/"+body.assistant_id)
    if not isinstance(assistant,dict) or assistant.get("id")!=body.assistant_id:
        raise HTTPException(404,"Vapi assistant not found")
    prefix=(org_or_404(oid)["name"]+" - CallPilot").casefold()
    if not str(assistant.get("name") or "").casefold().startswith(prefix):
        raise HTTPException(403,"Assistant does not belong to this business")
    other=api("agent_configs",params={"provider_agent_id":"eq."+body.assistant_id,
        "select":"organization_id","limit":10})
    if any(x["organization_id"]!=oid for x in other):
        raise HTTPException(409,"This assistant is already linked to another workspace")
    api("agent_configs","PATCH",params={"organization_id":"eq."+oid},
        data={"provider_agent_id":body.assistant_id},prefer="return=minimal")
    return {"linked":True,"assistant_id":body.assistant_id,"name":assistant.get("name")}

def sync_org_calls(oid:str,limit:int=50):
    config=agent_config(oid)
    assistant=config.get("provider_agent_id")
    if not assistant:
        # Safely auto-discover only one explicitly named CallPilot assistant for this workspace.
        business=org_or_404(oid)
        available=vapi("GET","/assistant",params={"limit":100})
        prefix=(business["name"]+" - CallPilot").casefold()
        matches=[x for x in available if isinstance(x,dict)
                 and isinstance(x.get("name"),str)
                 and x["name"].casefold().startswith(prefix)]
        if len(matches)==1:
            proposed=matches[0]["id"]
            others=api("agent_configs",params={"provider_agent_id":"eq."+proposed,
                "select":"organization_id","limit":10})
            if not any(x["organization_id"]!=oid for x in others):
                api("agent_configs","PATCH",params={"organization_id":"eq."+oid},
                    data={"provider_agent_id":proposed},prefer="return=minimal")
                assistant=proposed
        if not assistant:
            raise HTTPException(409,"Choose the correct Vapi assistant under Connect assistant, then sync calls")
    result=vapi("GET","/call",params={"assistantId":assistant,"limit":limit})
    if not isinstance(result,list):
        raise HTTPException(502,"Unexpected Vapi call list format")
    count=0
    for item in result:
        if not isinstance(item,dict) or item.get("assistantId")!=assistant:
            continue
        if item.get("status") not in ("ended","completed"):
            continue
        # List results may lack artifacts; obtain a full call record for each call.
        detail=vapi("GET","/call/"+item["id"])
        if detail.get("assistantId")!=assistant:continue
        persist(oid,detail)
        count+=1
    return {"synced":count,"checked":len(result),"assistant_id":assistant}

@router.post("/calls/sync")
def sync_calls(oid:str,user=Depends(current_user)):
    membership(oid,user,("owner","admin"))
    return sync_org_calls(oid)

def configure_analysis(oid:str):
    """Idempotently attach factual structured analysis to a linked Vapi assistant."""
    aid=agent_config(oid).get("provider_agent_id")
    if not aid:raise HTTPException(409,"Connect your Vapi assistant first")
    assistant=vapi("GET","/assistant/"+aid)
    existing=((assistant.get("artifactPlan") or {}).get("structuredOutputIds") or [])
    output_name="CallPilot Lead Intake - "+oid[:8]
    # First inspect the actual outputs attached to this assistant. Provider list
    # APIs can be paginated; don't create a duplicate on every deployment.
    for sid in existing:
        if not isinstance(sid,str):continue
        output=vapi("GET","/structured-output/"+sid)
        if isinstance(output,dict) and output.get("name")==output_name:
            return {"enabled":True,"existing":True,"structured_output_id":sid,
                    "message":"CallPilot AI call analysis is enabled"}
    all_outputs=vapi("GET","/structured-output")
    if isinstance(all_outputs,dict):
        all_outputs=all_outputs.get("data") or all_outputs.get("results") or all_outputs.get("items") or []
    matches=[x for x in all_outputs if isinstance(x,dict) and x.get("name")==output_name] if isinstance(all_outputs,list) else []
    if matches:
        sid=matches[0]["id"]
        if sid not in existing:
            vapi("PATCH","/assistant/"+aid,body={"artifactPlan":{
                **(assistant.get("artifactPlan") or {}),
                "structuredOutputIds":existing+[sid]}})
        return {"enabled":True,"existing":True,"structured_output_id":sid,"message":"CallPilot AI call analysis is enabled"}
    schema={"type":"object","properties":{
        "lead_stage":{"type":"string","enum":["hot","warm","general","not_qualified","needs_review"],
                      "description":"Hot: concrete intent, quantity or purchasing timeline AND asks next steps. Warm: interest but not ready. General: information only. Not qualified: mismatched request. Needs review: insufficient or conflicting evidence."},
        "reason":{"type":"string","description":"Cite a short piece of evidence stated by the caller. Do not guess."},
        "summary":{"type":"string","description":"2 sentence factual call summary; no invented details."},
        "caller_name":{"type":"string","description":"Name if explicitly provided, otherwise empty."},
        "product_interest":{"type":"string","description":"Requested product if explicitly mentioned."},
        "quantity_interest":{"type":"string","description":"Requested quantity if stated."},
        "destination":{"type":"string","description":"Shipment destination if stated."},
        "purchase_timeline":{"type":"string","description":"Purchasing timeline if stated."},
        "follow_up_needed":{"type":"boolean","description":"Caller explicitly requests follow-up or clear sales follow-up is needed."}
    },"required":["lead_stage","reason","summary"],
       "additionalProperties":False}
    created=vapi("POST","/structured-output",body={
        "name":output_name,"type":"ai",
        "description":"Extract factual lead intent and call summary from the completed call. Only include details explicitly mentioned by the caller. Never treat a caller as identity verified. Use needs_review when evidence is unclear.",
        "conditions":[{"type":"minCallDuration","seconds":10}],
        "schema":schema})
    sid=created.get("id")
    if not sid:raise HTTPException(502,"Vapi did not return a structured output ID")
    vapi("PATCH","/assistant/"+aid,body={"artifactPlan":{
        **(assistant.get("artifactPlan") or {}),
        "structuredOutputIds":existing+[sid]}})
    return {"enabled":True,"existing":False,"structured_output_id":sid,"message":"Post-call qualification enabled for future calls."}

@router.post("/calls/enable-analysis")
def enable_analysis(oid:str,user=Depends(current_user)):
    membership(oid,user,("owner","admin"))
    return configure_analysis(oid)
