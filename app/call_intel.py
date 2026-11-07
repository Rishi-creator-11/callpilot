"""Tenant-scoped business call intelligence and Vapi synchronization.

Only server-side Vapi/Supabase credentials are used. Never infer that a buyer
is verified from their statements or a model-generated score.
"""
from __future__ import annotations
import os, re, uuid
from datetime import datetime, timedelta, timezone
from typing import Literal
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from app.account import current_user, membership
from app.cloud import api, org_or_404
from app.common import wrapup_hook, WRAPUP_HOOK_NAME, MAX_CALL_SECONDS

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

def owns_assistant(assistant,oid:str)->bool:
    """Tenant ownership is an immutable server-set tag, never a display-name match."""
    meta=assistant.get("metadata") if isinstance(assistant,dict) else None
    return isinstance(meta,dict) and meta.get("callpilot_org")==oid

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
        "select":"id,lead_source,lead_stage,lead_reason,follow_up_needed,follow_up_status,follow_up_note,follow_up_due_at,last_reviewed_by,caller_phone,caller_name,product_interest"})
    if existing:
        previous=existing[0]
        if previous["lead_source"]=="human_reviewed":
            for k in ("lead_source","lead_stage","lead_reason","follow_up_needed"):
                data[k]=previous[k]
        # Keep caller-confirmed details captured by live voice tools even
        # when post-call AI analysis omits them.
        confirmed_by_voice=(previous.get("lead_reason") or "").startswith(
            "Caller explicitly requested a callback about:")
        for k in ("caller_phone","caller_name","product_interest"):
            if previous.get(k) and (
                not data.get(k) or confirmed_by_voice or previous.get("lead_source")=="human_reviewed"):
                data[k]=previous[k]
        if previous.get("follow_up_needed") is True:
            data["follow_up_needed"]=True
        if previous.get("lead_source")=="ai_suggested" and data.get("lead_source")=="unreviewed":
            data["lead_source"]="ai_suggested"
            data["lead_reason"]=previous.get("lead_reason")
        for k in ("follow_up_status","follow_up_note","follow_up_due_at","last_reviewed_by"):
            if previous.get(k) is not None:data[k]=previous[k]
    return api("call_insights","POST",params={"on_conflict":"organization_id,provider_call_id"},
        data=data,prefer="resolution=merge-duplicates,return=minimal")

@router.get("/calls")
def calls(oid: str, stage: str = "all", search: str = "",
          days: int = Query(30, ge=1, le=365),
          start: datetime | None = None, end: datetime | None = None,
          user=Depends(current_user)):
    membership(oid, user)
    custom_range = start is not None or end is not None
    if custom_range and (
        start is None or end is None or start.utcoffset() is None or end.utcoffset() is None
        or not timedelta(0) < end - start <= timedelta(days=367)
    ):
        raise HTTPException(422, "Choose an ordered date range of one year or less.")

    query = {
        "organization_id": "eq." + oid,
        "select": "id,provider_call_id,caller_phone,caller_name,started_at,duration_seconds,summary,lead_stage,lead_source,lead_reason,follow_up_needed,follow_up_status,product_interest,direction",
        "order": "started_at.desc.nullslast,id.desc",
        "limit": "301",
    }
    if custom_range:
        query["and"] = f"(started_at.gte.{start.isoformat()},started_at.lt.{end.isoformat()})"
    else:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        query["or"] = f"(started_at.gte.{cutoff.isoformat()},started_at.is.null)"
    rows = api("call_insights", params=query)
    period = rows[:300]
    term = search.casefold().strip()
    filtered = [
        row for row in period
        if (stage == "all" or row["lead_stage"] == stage)
        and (not term or any(term in str(row.get(field) or "").casefold() for field in (
            "caller_phone", "caller_name", "summary", "product_interest", "lead_stage",
        )))
    ]
    durations = [r["duration_seconds"] for r in period if r["duration_seconds"] is not None]
    return {
        "calls": filtered,
        "total": len(period),
        "truncated": len(rows) > len(period),
        "unique_callers": len({r["caller_phone"] for r in period if r["caller_phone"]}),
        "average_duration_seconds": round(sum(durations) / len(durations)) if durations else None,
        "counts": {key: sum(r["lead_stage"] == key for r in period) for key in VALID_STAGES},
        "follow_ups": sum(r["follow_up_needed"] and r["follow_up_status"] != "done" for r in period),
        "latest_sync": max((r["started_at"] for r in period if r["started_at"]), default=None),
    }


@router.get("/calls/{call_id}")
def call_detail(oid:str,call_id:uuid.UUID,user=Depends(current_user)):
    membership(oid,user)
    rows=api("call_insights",params={"organization_id":"eq."+oid,"id":"eq."+str(call_id),
        "select":"*"})
    if not rows:raise HTTPException(404,"Call not found")
    result=rows[0]
    result["has_recording"]=bool(result.get("recording_url"))
    result.pop("recording_url",None)
    return result

@router.get("/calls/{call_id}/recording")
async def call_recording(oid:str,call_id:uuid.UUID,user=Depends(current_user)):
    """Authorize playback per workspace and fetch a fresh recording via Vapi.

    Vapi's stored artifact URLs are private and expire; never return them to
    clients or expose the Vapi API key. The browser downloads audio with its
    existing Supabase session and plays a local Blob URL.
    """
    membership(oid,user)
    rows=api("call_insights",params={
        "organization_id":"eq."+oid,"id":"eq."+str(call_id),
        "select":"provider_call_id,assistant_id,provider_status","limit":1})
    if not rows:
        raise HTTPException(404,"Call not found")
    call=rows[0]
    key=os.getenv("VAPI_API_KEY","")
    if not key:
        raise HTTPException(503,"Call recording service is not connected")
    try:
        async with httpx.AsyncClient(timeout=55,follow_redirects=True) as client:
            async with client.stream("GET",
                "https://api.vapi.ai/call/"+call["provider_call_id"]+"/mono-recording",
                headers={"Authorization":"Bearer "+key,"Accept":"audio/*"}) as upstream:
                if upstream.status_code in (404,410):
                    raise HTTPException(404,"Recording is not available for this call")
                if upstream.status_code>=400:
                    raise HTTPException(502,"Vapi could not retrieve this recording")
                media_type=upstream.headers.get("content-type","").split(";")[0].strip().lower()
                if media_type not in ("audio/wav","audio/x-wav","audio/wave",
                                      "audio/mpeg","audio/mp3","audio/mp4",
                                      "application/octet-stream","binary/octet-stream"):
                    raise HTTPException(502,"Vapi returned an unexpected recording format")
                if int(upstream.headers.get("content-length") or 0)>60_000_000:
                    raise HTTPException(413,"Recording is too large for browser playback")
                chunks=[];size=0
                async for chunk in upstream.aiter_bytes(chunk_size=65536):
                    size+=len(chunk)
                    if size>60_000_000:
                        raise HTTPException(413,"Recording is too large for browser playback")
                    chunks.append(chunk)
    except httpx.TimeoutException:
        raise HTTPException(504,"Recording download timed out")
    except httpx.RequestError:
        raise HTTPException(503,"Recording service is temporarily unavailable")
    if not chunks:
        raise HTTPException(404,"Recording file is empty")
    return Response(content=b"".join(chunks),
        media_type="audio/mpeg" if media_type in ("audio/mpeg","audio/mp3") else
                   "audio/mp4" if media_type=="audio/mp4" else "audio/wav",
        headers={"Cache-Control":"private, no-store","X-Content-Type-Options":"nosniff",
                 "Content-Disposition":"inline; filename=call-recording"})

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
    return {"assistants":[{"id":x["id"],"name":x.get("name") or "Unnamed assistant"}
             for x in result if isinstance(x,dict) and isinstance(x.get("id"),str)
             and owns_assistant(x,oid)],
            "linked":agent_config(oid).get("provider_agent_id")}

class LinkAssistant(BaseModel):
    assistant_id:str=Field(min_length=5,max_length=150)

@router.post("/voice-assistants/link")
def link_voice_assistant(oid:str,body:LinkAssistant,user=Depends(current_user)):
    membership(oid,user,("owner","admin"))
    assistant=vapi("GET","/assistant/"+body.assistant_id)
    if not isinstance(assistant,dict) or assistant.get("id")!=body.assistant_id:
        raise HTTPException(404,"Vapi assistant not found")
    if not owns_assistant(assistant,oid):
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
        # Auto-discover only one assistant that CallPilot itself tagged for this workspace.
        available=vapi("GET","/assistant",params={"limit":100})
        matches=[x for x in available if isinstance(x,dict) and owns_assistant(x,oid)]
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

def request_call_analysis(oid:str,provider_call_id:str):
    """Queue Vapi structured output for an already completed call exactly once."""
    from datetime import timedelta
    rows=api("call_insights",params={
        "organization_id":"eq."+oid,"provider_call_id":"eq."+provider_call_id,
        "select":"id,analysis_requested_at,lead_source","limit":1})
    if not rows:raise HTTPException(404,"Call has not been imported")
    row=rows[0]
    if row.get("analysis_requested_at"):
        return {"queued":False,"already_requested":True}
    cfg=configure_analysis(oid)
    sid=cfg.get("structured_output_id")
    if not sid:raise HTTPException(503,"Call analysis is not configured")
    result=vapi("POST","/structured-output/run",body={
        "callIds":[provider_call_id],"structuredOutputId":sid,"previewEnabled":False})
    api("call_insights","PATCH",params={"organization_id":"eq."+oid,
        "provider_call_id":"eq."+provider_call_id},
        data={"analysis_requested_at":datetime.now(timezone.utc).isoformat(),
              "analysis_workflow_id":result.get("workflowId") if isinstance(result,dict) else None},
        prefer="return=minimal")
    return {"queued":True,"message":"Vapi analysis requested. Sync calls after processing."}

@router.post("/calls/{call_id}/analyze")
def analyze_past_call(oid:str,call_id:uuid.UUID,user=Depends(current_user)):
    membership(oid,user,("owner","admin"))
    rows=api("call_insights",params={"organization_id":"eq."+oid,"id":"eq."+str(call_id),
        "select":"provider_call_id","limit":1})
    if not rows:raise HTTPException(404,"Call not found")
    return request_call_analysis(oid,rows[0]["provider_call_id"])

def refresh_receptionist_prompt(oid:str, *, use_saved_greeting=False):
    """Synchronize platform conversation policy to already-created managed assistants.

    Preserve the existing voice, tools, recording/analysis settings, and other
    system/developer messages. Skip external assistants not tagged to this tenant.
    """
    from app.cloud import org_or_404,api as cloud_api,receptionist_prompt
    from app.identity import SPEECH_FIELDS, transcriber as speech_transcriber, voice_config
    cfg=cloud_api("agent_configs",params={"organization_id":"eq."+oid,
        "select":"provider_agent_id,agent_name,greeting,voice_leads_enabled,voice_booking_enabled,voice_catalog_enabled,voice_order_requests_enabled,"+SPEECH_FIELDS,"limit":1})
    if not cfg or not cfg[0].get("provider_agent_id"):
        return {"updated":False,"reason":"no_assistant"}
    aid=cfg[0]["provider_agent_id"]
    assistant=vapi("GET","/assistant/"+aid)
    if assistant.get("id")!=aid or not owns_assistant(assistant,oid):
        return {"updated":False,"reason":"not_managed_assistant"}
    model=assistant.get("model") or {}
    if not any(isinstance(t,dict) and (t.get("function") or {}).get("name")=="lookup_approved_knowledge"
               for t in (model.get("tools") or [])):
        return {"updated":False,"reason":"missing_semantic_tool"}
    messages=model.get("messages") or []
    if not isinstance(messages,list) or not any(
        isinstance(m,dict) and m.get("role")=="system" for m in messages):
        return {"updated":False,"reason":"no_system_message"}
    org=org_or_404(oid)
    desired=receptionist_prompt(oid,org["name"],cfg[0].get("agent_name") or "Alex")
    first=True
    next_messages=[]
    for message in messages:
        if isinstance(message,dict) and message.get("role")=="system" and first:
            next_messages.append(dict(message,content=desired))
            first=False
        else:
            next_messages.append(message)
    # Hang-up requires an actual Vapi endCall tool, not just instructions
    # to say goodbye. Preserve all other provider tools and custom settings.
    existing_tools=model.get("tools") or []
    if not isinstance(existing_tools,list):
        return {"updated":False,"reason":"invalid_tool_configuration"}
    from app.voice_actions import MANAGED_TOOLS,voice_tool_definitions
    flags=cfg[0]
    credential=os.getenv("VAPI_CREDENTIAL_ID","")
    if any(flags.get(k) for k in ("voice_leads_enabled","voice_booking_enabled","voice_catalog_enabled","voice_order_requests_enabled")) and not credential:
        raise HTTPException(503,"Vapi private tool credential is not configured")
    base_tools=[t for t in existing_tools if not (isinstance(t,dict) and
                (t.get("function") or {}).get("name") in MANAGED_TOOLS)]
    end_tool=next((t for t in base_tools if isinstance(t,dict) and t.get("type")=="endCall"),
                  {"type":"endCall"})
    next_tools=[t for t in base_tools if not (isinstance(t,dict) and t.get("type")=="endCall")]
    next_tools.extend(voice_tool_definitions(oid,credential,flags))
    next_tools.append(end_tool)
    transcriber=speech_transcriber(cfg[0])
    def matches_transcriber(candidate):
        current=candidate or {}
        return (all(current.get(key)==transcriber[key] for key in ("provider","model","language"))
                and (current.get("keyterm") or [])==(transcriber.get("keyterm") or []))
    prior_voice=assistant.get("voice") or {}
    desired_voice=voice_config(cfg[0],prior_voice)
    def matches_voice(candidate):
        current=candidate or {}
        if desired_voice.get("provider")=="vapi" and desired_voice.get("version")==2:
            return all(current.get(key)==desired_voice.get(key) for key in ("provider","voiceId","version","language"))
        return current==desired_voice
    goodbye="Thanks for calling. Goodbye."
    # Defer only the opening greeting, not ordinary conversational responses.
    # The scheduled hook uses the exact existing business-specific greeting.
    from app.common import opening_hook, OPENING_HOOK_NAME
    greeting=(cfg[0].get("greeting") if use_saved_greeting else assistant.get("firstMessage"))
    if not isinstance(greeting,str) or not greeting.strip():
        return {"updated":False,"reason":"missing_first_greeting"}
    prior_hooks=assistant.get("hooks") or []
    if not isinstance(prior_hooks,list):
        return {"updated":False,"reason":"invalid_hooks"}
    next_hooks=[h for h in prior_hooks if not (
        isinstance(h,dict) and h.get("name")==OPENING_HOOK_NAME)]
    next_hooks.insert(0,opening_hook(greeting))
    if (next_messages==messages and next_tools==existing_tools
            and assistant.get("firstMessage")==greeting
            and assistant.get("endCallMessage")==goodbye
            and assistant.get("firstMessageMode")=="assistant-waits-for-user"
            and next_hooks==prior_hooks
            and matches_transcriber(assistant.get("transcriber"))
            and matches_voice(prior_voice)):
        return {"updated":False,"reason":"already_current"}
    new_model=dict(model,messages=next_messages,tools=next_tools)
    vapi("PATCH","/assistant/"+aid,body={
        "model":new_model,"endCallMessage":goodbye,
        "firstMessage":greeting,
        "firstMessageMode":"assistant-waits-for-user",
        "transcriber":transcriber,"voice":desired_voice,
        "hooks":next_hooks})
    verified=vapi("GET","/assistant/"+aid)
    saved_model=verified.get("model") or {}
    saved=saved_model.get("messages") or []
    saved_tools=saved_model.get("tools") or []
    if (not any(m.get("role")=="system" and m.get("content")==desired
                for m in saved if isinstance(m,dict))
        or not any(t.get("type")=="endCall" for t in saved_tools if isinstance(t,dict))
        or verified.get("endCallMessage")!=goodbye
        or verified.get("firstMessage")!=greeting
        or verified.get("firstMessageMode")!="assistant-waits-for-user"
        or not matches_transcriber(verified.get("transcriber"))
        or not matches_voice(verified.get("voice"))
        or any((t.get("function") or {}).get("name") not in [
            (z.get("function") or {}).get("name") for z in saved_tools if isinstance(z,dict)]
            for t in next_tools if isinstance(t,dict) and (t.get("function") or {}).get("name") in MANAGED_TOOLS)
        or not any(isinstance(h,dict) and h.get("name")==OPENING_HOOK_NAME
                   and h.get("options",{}).get("seconds")==1
                   and any(a.get("exact")==greeting for a in h.get("do",[]) if isinstance(a,dict))
                   for h in (verified.get("hooks") or []))):
        raise HTTPException(502,"Vapi did not confirm voice policy and delayed greeting")
    return {"updated":True,"reason":"voice_policy_and_one_second_opening_synced"}

def disable_knowledge_filler(oid:str):
    """Silence Vapi's default request-start filler on approved FAQ lookups.

    Changes only the linked assistant's inline knowledge tool; leaves its voice,
    prompts, providers, credentials, other tools and phone number unchanged.
    """
    aid=agent_config(oid).get("provider_agent_id")
    if not aid:return {"updated":False,"reason":"no_linked_assistant"}
    assistant=vapi("GET","/assistant/"+aid)
    model=assistant.get("model") or {}
    tools=model.get("tools") if isinstance(model,dict) else None
    if not isinstance(tools,list):
        return {"updated":False,"reason":"no_inline_model_tools"}
    modified=[]
    changed=False
    for item in tools:
        if not isinstance(item,dict):
            modified.append(item)
            continue
        name=(item.get("function") or {}).get("name")
        if name!="lookup_approved_knowledge":
            modified.append(item)
            continue
        tool=dict(item)
        existing=[m for m in (tool.get("messages") or []) if isinstance(m,dict)
                  and m.get("type") not in ("request-start","request-response-delayed")]
        messages=[{"type":"request-start","content":""},
                  {"type":"request-response-delayed",
                   "content":"I'm still checking that information.",
                   "timingMilliseconds":4000}]
        desired=existing+messages
        if tool.get("messages")!=desired:
            tool["messages"]=desired
            changed=True
        modified.append(tool)
    if not any(isinstance(t,dict) and (t.get("function") or {}).get("name")=="lookup_approved_knowledge" for t in tools):
        return {"updated":False,"reason":"knowledge_tool_not_found"}
    if not changed:
        return {"updated":False,"reason":"already_configured"}
    patch_model=dict(model)
    patch_model["tools"]=modified
    # Vapi assistant updates accept the complete existing model configuration.
    vapi("PATCH","/assistant/"+aid,body={"model":patch_model})
    updated=vapi("GET","/assistant/"+aid)
    saved=(updated.get("model") or {}).get("tools") or []
    verified=any(isinstance(t,dict)
      and (t.get("function") or {}).get("name")=="lookup_approved_knowledge"
      and any(m.get("type")=="request-start" and m.get("content")==""
              for m in (t.get("messages") or []) if isinstance(m,dict))
      for t in saved)
    if not verified:raise HTTPException(502,"Vapi did not confirm silent knowledge lookups")
    return {"updated":True,"reason":"silent_lookup_verified"}


def apply_call_duration_policy(oid:str):
    """Cap this business's actual Vapi assistant at 240 seconds.

    At 215 seconds the agent receives a gentle wrap-up cue.
    The 240-second timeout is provider-enforced, even while speaking.
    Retains existing hooks and never alters another assistant.
    """
    aid=agent_config(oid).get("provider_agent_id")
    if not aid:
        return {"configured":False,"reason":"no_linked_assistant"}
    assistant=vapi("GET","/assistant/"+aid)
    if not isinstance(assistant,dict) or assistant.get("id")!=aid:
        raise HTTPException(502,"Vapi assistant could not be verified")
    prior=assistant.get("hooks") or []
    if not isinstance(prior,list):
        prior=[]
    existing=[hook for hook in prior if not (
        isinstance(hook,dict) and hook.get("name")==WRAPUP_HOOK_NAME)]
    target=existing+[wrapup_hook()]
    meta=assistant.get("metadata") if isinstance(assistant.get("metadata"),dict) else {}
    if meta.get("callpilot_org") not in (None,oid):
        raise HTTPException(409,"Assistant is tagged to another workspace")
    tagged=dict(meta,callpilot_org=oid)
    if (assistant.get("maxDurationSeconds")==MAX_CALL_SECONDS and prior==target
            and meta.get("callpilot_org")==oid):
        return {"configured":True,"changed":False,"max_duration_seconds":MAX_CALL_SECONDS}
    vapi("PATCH","/assistant/"+aid,
         body={"maxDurationSeconds":MAX_CALL_SECONDS,"hooks":target,"metadata":tagged})
    verified=vapi("GET","/assistant/"+aid)
    if verified.get("maxDurationSeconds")!=240:
        raise HTTPException(502,"Vapi did not confirm the 4-minute call limit")
    return {"configured":True,"changed":True,"max_duration_seconds":MAX_CALL_SECONDS}
