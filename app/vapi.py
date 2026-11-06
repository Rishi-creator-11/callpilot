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
    if msg.get("type")!="end-of-call-report": return {"ok":True,"ignored":True}
    call=msg.get("call") or {}
    if not isinstance(call,dict) or not isinstance(call.get("id"),str):
        raise HTTPException(400,"Missing provider call ID")
    org_or_404(organization_id)
    # Idempotent insert. No call transcript, recording URL, caller phone, or provider secrets.
    api("call_events","POST",
        params={"on_conflict":"organization_id,provider_call_id,event_type"},
        data={"organization_id":organization_id,"provider_call_id":call["id"],"event_type":"end-of-call-report",
              "outcome":"received"},
        prefer="resolution=ignore-duplicates,return=minimal")
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
    # Fail closed on unknown workspaces.
    org_or_404(organization_id)
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
        if PRICE.search(question):
            answer=NO_PRICE
        else:
            try:
                fact,meta=retrieve(organization_id,question)
                answer=fact["answer"] if fact else UNKNOWN
                if MONEY.search(answer):answer=NO_PRICE
            except Exception:
                answer=UNKNOWN
        # Tool output must be a flat string for Vapi's parser.
        results.append({"toolCallId":call_id,"result":" ".join(answer.split())})
    return {"results":results}
