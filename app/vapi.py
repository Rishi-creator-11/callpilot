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
