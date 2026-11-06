"""CallPilot Supabase persistence. Internal admin API only; never send secret key to browser."""
import os, hmac, re, uuid
from datetime import datetime, timezone
import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from app.main import authorize, PRICE, MONEY, NO_PRICE, UNKNOWN

router = APIRouter(prefix="/api/cloud", dependencies=[Depends(authorize)], tags=["persistent-workspaces"])
BASE = os.getenv("SUPABASE_URL", "").rstrip("/")
KEY = os.getenv("SUPABASE_SECRET_KEY", "")
def require_config():
    if not BASE or not KEY or not BASE.startswith("https://"):
        raise HTTPException(503,"Supabase cloud storage not configured")
def api(table, method="GET", params=None, data=None, prefer=None):
    require_config()
    headers={"apikey":KEY,"Authorization":"Bearer "+KEY,"Content-Type":"application/json"}
    if prefer: headers["Prefer"]=prefer
    try:
        with httpx.Client(timeout=12) as client:
            resp=client.request(method, BASE+"/rest/v1/"+table, headers=headers, params=params, json=data)
        if resp.status_code>=400:
            raise HTTPException(502,"Cloud database request failed: "+str(resp.status_code))
        return resp.json() if resp.content else []
    except httpx.RequestError:
        raise HTTPException(503,"Cloud database unavailable")
def org_or_404(org_id):
    x=api("organizations", params={"id":"eq."+org_id,"select":"id,name,industry,timezone,status"})
    if not x: raise HTTPException(404,"Business not found")
    return x[0]
class Organization(BaseModel):
    name:str=Field(min_length=2,max_length=120)
    industry:str=Field(default="General",max_length=80)
    timezone:str=Field(default="America/New_York",max_length=80)
class Fact(BaseModel):
    question:str=Field(min_length=3,max_length=500)
    answer:str=Field(min_length=2,max_length=2500)
    approved:bool=False
class Ask(BaseModel):
    question:str=Field(min_length=1,max_length=1000)
class Request(BaseModel):
    name:str=Field(min_length=1,max_length=100)
    phone:str=Field(min_length=3,max_length=100)
    service:str=Field(min_length=1,max_length=200)
    requested_time:datetime
@router.get("/status")
def status():
    return {"configured":bool(BASE and KEY),"mode":"server-only","live_calls":False,"confirmed_booking":False}
@router.get("/businesses")
def businesses():
    return api("organizations",params={"select":"id,name,industry,timezone,status,created_at","order":"created_at.desc","limit":100})
@router.post("/businesses")
def business_create(body:Organization):
    oid=str(uuid.uuid4())
    row=api("organizations","POST",data={"id":oid,**body.model_dump(),"status":"draft"},prefer="return=representation")
    api("agent_configs","POST",data={"organization_id":oid,"greeting":f"Hello, thanks for calling {body.name}. I'm the AI assistant. How may I help?"},prefer="return=minimal")
    return row[0]
@router.get("/businesses/{oid}/knowledge")
def knowledge(oid:str):
    org_or_404(oid)
    return api("knowledge_facts",params={"organization_id":"eq."+oid,"select":"id,question,answer,approved_at,created_at","order":"created_at.desc"})
@router.post("/businesses/{oid}/knowledge")
def knowledge_add(oid:str,body:Fact):
    org_or_404(oid)
    if MONEY.search(body.answer) or PRICE.search(body.question):
        raise HTTPException(400,"Monetary information cannot be stored in public agent knowledge")
    data={"organization_id":oid,"question":body.question,"answer":body.answer}
    # Approval is intentionally withheld until user-authenticated approval workflow is implemented.
    row=api("knowledge_facts","POST",data=data,prefer="return=representation")
    return {"id":row[0]["id"],"approved":False,"requires_review":True}
@router.post("/businesses/{oid}/simulate")
def simulate(oid:str,body:Ask):
    org_or_404(oid)
    if PRICE.search(body.question):return {"answer":NO_PRICE,"source":"policy","human_needed":True}
    rows=api("knowledge_facts",params={"organization_id":"eq."+oid,"approved_at":"not.is.null","select":"question,answer"})
    for row in rows:
        if row["question"].strip().casefold()==body.question.strip().casefold():
            if MONEY.search(row["answer"]): return {"answer":NO_PRICE,"source":"policy","human_needed":True}
            return {"answer":row["answer"],"source":"approved_fact","human_needed":False}
    return {"answer":UNKNOWN,"source":"unknown","human_needed":True}
@router.post("/businesses/{oid}/booking-requests")
def booking(oid:str,body:Request):
    org_or_404(oid)
    if body.requested_time.tzinfo is None or body.requested_time <= datetime.now(timezone.utc):
        raise HTTPException(422,"Time must be future and include timezone")
    row=api("appointment_requests","POST",data={"organization_id":oid,"caller_name":body.name,"caller_contact":body.phone,"service":body.service,"requested_time":body.requested_time.isoformat(),"status":"requested"},prefer="return=representation")
    return {"id":row[0]["id"],"status":"requested","confirmed":False}
