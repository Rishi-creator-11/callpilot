from app.semantic import index_fact, reindex_approved, retrieve, enabled
"""Customer self-service API: verified Supabase identity and tenant membership."""
import os, uuid, httpx
from fastapi import APIRouter, Header, Depends, HTTPException
from pydantic import BaseModel, Field
from app.cloud import api, PRICE, MONEY, NO_PRICE, UNKNOWN

router=APIRouter(prefix="/api/account",tags=["customer"])
# Only the owner's Call Pilot marketing workspace may publish its fixed plans.
# Never allow other tenants to bypass price protections.
CALLPILOT_ORG="b550c0af-7d9c-4231-8eec-cb9f6ba9e724"
PLAN_QUESTION="What are CallPilot's plans?"
PLAN_ANSWER=("Starter is $49 per month, Growth is $99 per month, and Business is "
             "$179 per month. Each plan has a $25 one-time setup fee. "
             "Applicable taxes are additional. We do not negotiate or offer special deals.")
def allowed_plan_fact(oid,question,answer):
    return oid==CALLPILOT_ORG and question.strip()==PLAN_QUESTION and answer.strip()==PLAN_ANSWER
def validate_fact(oid,question,answer):
    if (PRICE.search(question) or MONEY.search(answer)) and not allowed_plan_fact(oid,question,answer):
        raise HTTPException(400,"Pricing is restricted to CallPilot's verified published plan FAQ.")

SUPA=lambda:os.getenv("SUPABASE_URL","").rstrip("/")
PUB=lambda:os.getenv("SUPABASE_PUBLISHABLE_KEY","")

def current_user(authorization:str|None=Header(default=None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401,"Sign in required")
    if not SUPA() or not PUB():raise HTTPException(503,"Authentication is not configured")
    token=authorization[7:]
    if len(token)<20 or len(token)>4096:raise HTTPException(401,"Invalid token")
    try:
        with httpx.Client(timeout=8) as c:
            r=c.get(SUPA()+"/auth/v1/user",headers={"apikey":PUB(),"Authorization":"Bearer "+token})
    except httpx.RequestError:raise HTTPException(503,"Authentication service unavailable")
    if r.status_code!=200:raise HTTPException(401,"Session invalid or expired")
    user=r.json()
    if not user.get("id") or not user.get("email_confirmed_at"):
        raise HTTPException(403,"Verified email required")
    return {"id":user["id"],"email":user.get("email")}

def membership(oid:str,user:dict,roles=("owner","admin","viewer")):
    members=api("organization_members",params={"organization_id":"eq."+oid,"user_id":"eq."+user["id"],"select":"role"})
    if not members or members[0]["role"] not in roles:raise HTTPException(404,"Workspace not found")
    return members[0]["role"]

class OrgCreate(BaseModel):
    name:str=Field(min_length=2,max_length=120)
    industry:str=Field(default="General",max_length=80)
class FactCreate(BaseModel):
    question:str=Field(min_length=3,max_length=500)
    answer:str=Field(min_length=2,max_length=2500)
class Question(BaseModel):
    question:str=Field(min_length=1,max_length=1000)
class AgentEdit(BaseModel):
    agent_name:str=Field(min_length=1,max_length=80)
    greeting:str=Field(min_length=10,max_length=500)

@router.get("/config")
def config():
    return {"supabase_url":SUPA() if PUB() else None,"supabase_publishable_key":PUB() or None,
            "billing_enabled":False,"signup_enabled":bool(PUB())}

@router.get("/me")
def me(user=Depends(current_user)):
    member=api("organization_members",params={"user_id":"eq."+user["id"],"select":"organization_id,role"})
    ids=[m["organization_id"] for m in member]
    organizations=api("organizations",params={"id":"in.("+",".join(ids)+")","select":"id,name,industry,status"}) if ids else []
    for org in organizations:
        org["role"]=next((m["role"] for m in member if m["organization_id"]==org["id"]),None)
    return {"email":user["email"],"organizations":organizations}

@router.post("/organizations")
def create_org(body:OrgCreate,user=Depends(current_user)):
    # Service role only; create atomic organization and membership using a restricted SQL RPC.
    oid=str(uuid.uuid4())
    try:
        rows=rpc("create_customer_workspace",{"p_org_id":oid,"p_user_id":user["id"],"p_name":body.name,"p_industry":body.industry})
    except HTTPException:raise
    return {"id":oid,"name":body.name,"status":"draft"}

def rpc(name,payload):
    import hmac
    secret=os.getenv("SUPABASE_SECRET_KEY","")
    if not secret:raise HTTPException(503,"Database is not configured")
    try:
        with httpx.Client(timeout=10) as c:
            r=c.post(SUPA()+"/rest/v1/rpc/"+name,
                headers={"apikey":secret,"Content-Type":"application/json"},
                json=payload)
    except httpx.RequestError:raise HTTPException(503,"Database unavailable")
    if r.status_code>=400:raise HTTPException(502,"Database operation failed")
    return r.json() if r.content else None

@router.get("/organizations/{oid}/agent")
def get_agent(oid:str,user=Depends(current_user)):
    membership(oid,user)
    a=api("agent_configs",params={"organization_id":"eq."+oid,"select":"agent_name,greeting,voice_provider,voice_id,voice_consent_verified_at"})
    return a[0] if a else {}

@router.put("/organizations/{oid}/agent")
def update_agent(oid:str,body:AgentEdit,user=Depends(current_user)):
    membership(oid,user,("owner","admin"))
    api("agent_configs","PATCH",params={"organization_id":"eq."+oid},
        data={"agent_name":body.agent_name,"greeting":body.greeting},prefer="return=minimal")
    return {"saved":True}

@router.get("/organizations/{oid}/knowledge")
def list_knowledge(oid:str,user=Depends(current_user)):
    membership(oid,user)
    return api("knowledge_facts",params={"organization_id":"eq."+oid,
        "select":"id,question,answer,approved_at,created_at","order":"created_at.desc"})

@router.post("/organizations/{oid}/knowledge")
def create_fact(oid:str,body:FactCreate,user=Depends(current_user)):
    membership(oid,user,("owner","admin"))
    validate_fact(oid,body.question,body.answer)
    f=api("knowledge_facts","POST",data={"organization_id":oid,"question":body.question,"answer":body.answer},prefer="return=representation")
    return {"id":f[0]["id"],"approved":False}

@router.post("/organizations/{oid}/knowledge/{fid}/approve")
def approve_fact(oid:str,fid:str,user=Depends(current_user)):
    membership(oid,user,("owner","admin"))
    facts=api("knowledge_facts",params={"organization_id":"eq."+oid,"id":"eq."+fid,"select":"question,answer"})
    if not facts:raise HTTPException(404,"Fact not found")
    validate_fact(oid,facts[0]["question"],facts[0]["answer"])
    from datetime import datetime,timezone
    fact=api("knowledge_facts",params={"organization_id":"eq."+oid,"id":"eq."+fid,
        "select":"id,question,answer"})[0]
    index_fact(oid,fid,fact["question"],approved_by=user["id"])
    return {"approved":True,"embedded":True}

@router.put("/organizations/{oid}/knowledge/{fid}")
def update_knowledge(oid:str,fid:str,body:FactCreate,user=Depends(current_user)):
    membership(oid,user,("owner","admin"))
    rows=api("knowledge_facts",params={"organization_id":"eq."+oid,"id":"eq."+fid,
        "select":"id,approved_at"})
    if not rows:raise HTTPException(404,"Knowledge entry not found")
    question,answer=body.question.strip(),body.answer.strip()
    validate_fact(oid,question,answer)
    # Index first; failed indexing must not publish stale or unindexed content.
    from app.semantic import embeddings,to_pgvector,MODEL
    from datetime import datetime,timezone
    vector=embeddings([question])[0]
    data={"question":question,"answer":answer,
          "question_embedding_384":to_pgvector(vector),"embedding_model":MODEL,
          "embedded_at":datetime.now(timezone.utc).isoformat()}
    api("knowledge_facts","PATCH",params={"organization_id":"eq."+oid,"id":"eq."+fid},
        data=data,prefer="return=minimal")
    return {"saved":True,"approved":bool(rows[0]["approved_at"]),"indexed":True}

@router.delete("/organizations/{oid}/knowledge/{fid}")
def delete_knowledge(oid:str,fid:str,user=Depends(current_user)):
    membership(oid,user,("owner","admin"))
    rows=api("knowledge_facts",params={"organization_id":"eq."+oid,"id":"eq."+fid,"select":"id"})
    if not rows:raise HTTPException(404,"Knowledge entry not found")
    api("knowledge_facts","DELETE",params={"organization_id":"eq."+oid,"id":"eq."+fid},
        prefer="return=minimal")
    return {"deleted":True}

@router.post("/organizations/{oid}/simulate")
def simulate(oid:str,body:Question,user=Depends(current_user)):
    membership(oid,user)
    if PRICE.search(body.question):
        if oid==CALLPILOT_ORG:
            import re
            if re.search(r"\\b(discount|negotiate|deal|waive|reduce|lower|free trial|custom price)\\b",body.question,re.I):
                return {"answer":"Published prices are fixed. Our team can discuss requirements, but I cannot negotiate or offer deals.","source":"policy"}
            return {"answer":PLAN_ANSWER,"source":"published_pricing"}
        return {"answer":NO_PRICE,"source":"policy"}
    # Lazy backfill for FAQs approved before embeddings were introduced.
    pending=api("knowledge_facts",params={"organization_id":"eq."+oid,
        "approved_at":"not.is.null","question_embedding_384":"is.null","select":"id","limit":1})
    if pending:
        reindex_approved(oid)
    fact,metadata=retrieve(oid,body.question)
    if fact:
        if MONEY.search(fact["answer"]):return {"answer":NO_PRICE,"source":"policy"}
        return {"answer":fact["answer"],"source":"approved_semantic",
                "matched_question":fact["question"],"similarity":metadata["similarity"]}
    return {"answer":UNKNOWN,"source":"unknown","reason":metadata["reason"]}

@router.get("/organizations/{oid}/bookings")
def bookings(oid:str,user=Depends(current_user)):
    membership(oid,user)
    return api("appointment_requests",params={"organization_id":"eq."+oid,"select":"id,caller_name,service,requested_time,status,created_at","order":"created_at.desc","limit":50})

@router.post("/organizations/{oid}/test-assistant")
def create_test_assistant(oid:str,user=Depends(current_user)):
    membership(oid,user,("owner","admin"))
    from app.cloud import provision_test_agent
    return provision_test_agent(oid)

class VoiceRequestInput(BaseModel):
    owner_contact:str=Field(min_length=5,max_length=160)
    provider_voice_id:str=Field(min_length=5,max_length=180)
    consent_acknowledged:bool

@router.get("/organizations/{oid}/voice-requests")
def voice_requests(oid:str,user=Depends(current_user)):
    membership(oid,user)
    return api("voice_requests",params={"organization_id":"eq."+oid,
        "select":"id,provider,status,owner_contact,created_at","order":"created_at.desc","limit":15})

@router.post("/organizations/{oid}/voice-requests")
def request_custom_voice(oid:str,body:VoiceRequestInput,user=Depends(current_user)):
    membership(oid,user,("owner","admin"))
    if not body.consent_acknowledged:
        raise HTTPException(422,"Voice owner consent must be acknowledged")
    previous=api("voice_requests",params={"organization_id":"eq."+oid,"status":"eq.pending","select":"id"})
    if previous:raise HTTPException(409,"A voice request is already awaiting verification")
    item=api("voice_requests","POST",data={"organization_id":oid,"requested_by":user["id"],
        "owner_contact":body.owner_contact,
        "provider_voice_id":body.provider_voice_id,
        "consent_acknowledged":True,"status":"pending"},prefer="return=representation")
    return {"id":item[0]["id"],"status":"pending",
            "message":"This request does not activate a voice. Verification happens through the voice provider."}

@router.get("/organizations/{oid}/semantic-status")
def semantic_status(oid:str,user=Depends(current_user)):
    membership(oid,user)
    facts=api("knowledge_facts",params={"organization_id":"eq."+oid,
        "approved_at":"not.is.null","select":"id,question,embedded_at","limit":1000})
    return {"configured":enabled(),"approved_facts":len(facts),
            "indexed_facts":sum(bool(f["embedded_at"]) for f in facts),
            "pending_index":sum(not f["embedded_at"] for f in facts)}

@router.post("/organizations/{oid}/knowledge/reindex")
def semantic_reindex(oid:str,user=Depends(current_user)):
    membership(oid,user,("owner","admin"))
    return reindex_approved(oid)

class WorkspaceUpdate(BaseModel):
    name:str=Field(min_length=2,max_length=120)
    industry:str=Field(min_length=2,max_length=80)

@router.patch("/organizations/{oid}/settings")
def workspace_settings(oid:str,body:WorkspaceUpdate,user=Depends(current_user)):
    membership(oid,user,("owner","admin"))
    api("organizations","PATCH",params={"id":"eq."+oid},
        data={"name":body.name.strip(),"industry":body.industry.strip()},prefer="return=minimal")
    return {"saved":True}

@router.get("/organizations/{oid}/team")
def workspace_team(oid:str,user=Depends(current_user)):
    membership(oid,user)
    members=api("organization_members",params={"organization_id":"eq."+oid,
        "select":"role,user_id","limit":100})
    return {"members":[{"role":m["role"],"is_you":m["user_id"]==user["id"]} for m in members],
            "note":"Team invitations are not available yet. Existing members are shown without exposing other users' private contact details."}
