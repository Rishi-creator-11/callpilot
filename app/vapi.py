"""Authenticated Vapi event receiver. Stores metadata only, never transcripts or recordings."""
import os, hmac, uuid
from fastapi import APIRouter, Request, Header, HTTPException
from app.cloud import api, org_or_404
router = APIRouter(tags=["vapi"])
@router.post("/webhooks/vapi/{organization_id}")
async def vapi_event(organization_id:str, request:Request, authorization:str|None=Header(default=None)):
    secret=os.getenv("VAPI_WEBHOOK_TOKEN","")
    if not secret or not authorization or not hmac.compare_digest(authorization,"Bearer "+secret):
        raise HTTPException(401,"Invalid webhook token")
    if int(request.headers.get("content-length","0") or 0)>131072:
        raise HTTPException(413,"Payload too large")
    try:
        msg=(await request.json()).get("message",{})
    except (ValueError,AttributeError):
        raise HTTPException(400,"Invalid webhook body")
    if not isinstance(msg,dict):raise HTTPException(400,"Invalid message")
    event_type=msg.get("type")
    if event_type not in ("end-of-call-report","status-update"):
        return {"ok":True,"ignored":True}
    call=msg.get("call") or {}
    if not isinstance(call,dict) or not isinstance(call.get("id"),str):
        raise HTTPException(400,"Missing provider call ID")
    org_or_404(organization_id)
    configured=api("agent_configs",params={"organization_id":"eq."+organization_id,
        "select":"provider_agent_id,provider_phone_id","limit":1})
    config=configured[0] if configured else {}
    if config.get("provider_agent_id") and call.get("assistantId") and call["assistantId"]!=config["provider_agent_id"]:
        raise HTTPException(403,"Assistant does not belong to this workspace")
    if event_type=="status-update":
        if msg.get("status")!="in-progress":
            return {"ok":True,"ignored":True}
        from datetime import datetime,timezone
        started=call.get("startedAt") or datetime.now(timezone.utc).isoformat()
        api("call_insights","POST",params={"on_conflict":"organization_id,provider_call_id"},
            data={"organization_id":organization_id,"provider_call_id":call["id"],
                  "provider":"vapi","assistant_id":call.get("assistantId"),
                  "phone_number_id":call.get("phoneNumberId"),
                  "started_at":started,"provider_status":"in-progress","direction":"inbound"},
            prefer="resolution=merge-duplicates,return=minimal")
        return {"ok":True,"usage_tracking":"started"}
    # Idempotent insert. No call transcript, recording URL, caller phone, or provider secrets.
    api("call_events","POST",
        params={"on_conflict":"organization_id,provider_call_id,event_type"},
        data={"organization_id":organization_id,"provider_call_id":call["id"],"event_type":"end-of-call-report",
              "outcome":"received"},
        prefer="resolution=ignore-duplicates,return=minimal")
    # Keep the business call dashboard in sync as events arrive.
    # Preserve server-side tenant verification and idempotent provider call ID.
    from app.call_intel import persist
    persist(organization_id,{**call,
        "artifact":msg.get("artifact") or call.get("artifact") or {},
        "analysis":msg.get("analysis") or call.get("analysis") or {},
        "endedAt":msg.get("endedAt") or call.get("endedAt"),
        "startedAt":msg.get("startedAt") or call.get("startedAt"),
        "endedReason":msg.get("endedReason") or call.get("endedReason"),
        "recordingUrl":msg.get("recordingUrl") or call.get("recordingUrl"),
        "transcript":msg.get("transcript") or call.get("transcript"),
    })
    # Create unique in-dashboard usage warnings as calls complete.
    try:
        from app.usage import record_threshold_alerts
        record_threshold_alerts(organization_id)
    except Exception:
        # A notification failure must not cause Vapi to retry/duplicate the call report.
        import logging
        logging.getLogger("uvicorn.error").warning(
            "callpilot_usage_alerts_failed org=%s",organization_id)
    return {"ok":True}

@router.post("/webhooks/vapi-tools/{organization_id}")
async def vapi_semantic_tool(organization_id:str,request:Request,authorization:str|None=Header(default=None)):
    """Private Vapi function-tool webhook. Tenant id comes from the server URL, never model arguments."""
    secret=os.getenv("VAPI_WEBHOOK_TOKEN","")
    if not secret or not authorization or not hmac.compare_digest(authorization,"Bearer "+secret):
        raise HTTPException(401,"Invalid Vapi tool credential")
    body=await request.body()
    if len(body)>65536:raise HTTPException(413,"Too much data")
    try:
        msg=__import__("json").loads(body).get("message",{})
    except (ValueError,AttributeError):
        raise HTTPException(400,"Invalid payload")
    if not isinstance(msg,dict) or msg.get("type")!="tool-calls":
        return {"results":[]}
    calls=msg.get("toolCallList",[])
    if not isinstance(calls,list) or len(calls)>5:
        raise HTTPException(400,"Invalid tool call list")
    # Fail closed on unknown workspaces and on calls handled by another workspace's assistant.
    org_or_404(organization_id)
    claimed=(msg.get("call") or {}).get("assistantId") if isinstance(msg.get("call"),dict) else None
    if claimed is not None:
        rows=api("agent_configs",params={"organization_id":"eq."+organization_id,
            "select":"provider_agent_id","limit":1})
        if not rows or rows[0].get("provider_agent_id")!=claimed:
            raise HTTPException(403,"Assistant does not belong to this workspace")
    from app.semantic import retrieve
    from app.cloud import PRICE, MONEY, NO_PRICE, UNKNOWN
    results=[]
    for call in calls:
        if not isinstance(call,dict):continue
        call_id=call.get("id")
        if not isinstance(call_id,str) or len(call_id)>120:continue
        func=call.get("function") or {}
        args=func.get("arguments") or {}
        if isinstance(args,str):
            try:args=__import__("json").loads(args)
            except ValueError:args={}
        if func.get("name")!="lookup_approved_knowledge":
            results.append({"toolCallId":call_id,"error":"Unsupported tool"});continue
        question=args.get("question","") if isinstance(args,dict) else ""
        if not isinstance(question,str) or not (1<=len(question)<=500):
            results.append({"toolCallId":call_id,"error":"Invalid question"});continue
        from app.account import CALLPILOT_ORG,PLAN_ANSWER
        if organization_id==CALLPILOT_ORG and PRICE.search(question):
            import re
            if re.search(r"\\b(discount|negotiate|deal|waive|reduce|lower|free trial|custom price)\\b",question,re.I):
                answer="Published prices are fixed. Our team can discuss requirements, but I cannot negotiate or offer deals."
            else:
                answer=PLAN_ANSWER
        elif PRICE.search(question):
            answer=NO_PRICE
        else:
            try:
                fact,meta=retrieve(organization_id,question)
                answer=fact["answer"] if fact else UNKNOWN
                if MONEY.search(answer) and organization_id!=CALLPILOT_ORG:answer=NO_PRICE
            except Exception:
                answer=UNKNOWN
        # Tool output must be a flat string for Vapi's parser.
        results.append({"toolCallId":call_id,"result":" ".join(answer.split())})
    return {"results":results}
