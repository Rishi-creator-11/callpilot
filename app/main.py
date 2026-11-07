"""CallPilot protected internal demo; no live booking or telephony is activated."""
from __future__ import annotations
import os, sqlite3, uuid, hmac, re
from pathlib import Path
from datetime import datetime, timezone
from fastapi import FastAPI, Header, HTTPException, Depends
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from app.common import authorize, PRICE, MONEY, NO_PRICE, UNKNOWN

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.getenv("DATA_DIR", str(ROOT / "data")))
DATA.mkdir(parents=True, exist_ok=True)
DB = DATA / "callpilot.db"
app = FastAPI(title="CallPilot", version="0.2.0")

def connect():
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db

with connect() as db:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS businesses(
      id TEXT PRIMARY KEY, name TEXT NOT NULL, industry TEXT NOT NULL,
      agent_name TEXT NOT NULL DEFAULT 'Alex',
      greeting TEXT NOT NULL DEFAULT 'Hello, I am the AI assistant. How can I help?',
      voice_provider TEXT NOT NULL DEFAULT 'standard',
      voice_id TEXT NOT NULL DEFAULT '',
      voice_owner_verified INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS knowledge(
      id TEXT PRIMARY KEY, business_id TEXT NOT NULL REFERENCES businesses(id),
      question TEXT NOT NULL, answer TEXT NOT NULL, approved INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS booking_requests(
      id TEXT PRIMARY KEY, business_id TEXT NOT NULL REFERENCES businesses(id),
      name TEXT NOT NULL, phone TEXT NOT NULL, service TEXT NOT NULL,
      requested_time TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'requested');
    """)

def uid(): return str(uuid.uuid4())
def ts(): return datetime.now(timezone.utc).isoformat()
def business_or_404(db, business_id):
    row = db.execute("SELECT * FROM businesses WHERE id=?", (business_id,)).fetchone()
    if not row: raise HTTPException(404, "Business not found")
    return row

class BusinessInput(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    industry: str = Field(default="General", max_length=80)

class AgentInput(BaseModel):
    name: str = Field(default="Alex", max_length=80)
    greeting: str = Field(min_length=10, max_length=500)
    voice_provider: str = Field(default="standard", pattern="^(standard|elevenlabs)$")
    voice_id: str = Field(default="", max_length=180)
    owner_verified: bool = False

class KnowledgeInput(BaseModel):
    question: str = Field(min_length=3, max_length=500)
    answer: str = Field(min_length=2, max_length=2500)
    approved: bool = False

class QuestionInput(BaseModel):
    question: str = Field(min_length=1, max_length=1000)

class BookingInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    phone: str = Field(min_length=3, max_length=100)
    service: str = Field(min_length=1, max_length=200)
    requested_time: str = Field(min_length=4, max_length=100)

@app.get("/health")
def health(): return {"status":"ok","mode":"internal-demo"}

@app.get("/", response_class=HTMLResponse)
def home():
    return (ROOT/"static"/"customer.html").read_text()

@app.get("/api/businesses", dependencies=[Depends(authorize)])
def businesses():
    with connect() as db: return [dict(r) for r in db.execute("SELECT * FROM businesses ORDER BY created_at DESC")]

@app.post("/api/businesses", dependencies=[Depends(authorize)])
def create_business(body: BusinessInput):
    id = uid()
    with connect() as db:
        db.execute("INSERT INTO businesses(id,name,industry,created_at) VALUES(?,?,?,?)",(id,body.name,body.industry,ts()))
        db.commit()
    return {"id":id,"name":body.name}

@app.put("/api/businesses/{bid}/agent", dependencies=[Depends(authorize)])
def agent(bid: str, body: AgentInput):
    if body.voice_provider=="elevenlabs" and (not body.owner_verified or not body.voice_id):
        raise HTTPException(400,"Verified voice-owner consent and voice ID required")
    with connect() as db:
        business_or_404(db,bid)
        db.execute("UPDATE businesses SET agent_name=?,greeting=?,voice_provider=?,voice_id=?,voice_owner_verified=? WHERE id=?",
            (body.name,body.greeting,body.voice_provider,body.voice_id,int(body.owner_verified),bid))
        db.commit()
    return {"saved":True,"voice_is_live":False}

@app.get("/api/businesses/{bid}/knowledge", dependencies=[Depends(authorize)])
def knowledge(bid: str):
    with connect() as db:
        business_or_404(db,bid)
        return [dict(x) for x in db.execute("SELECT * FROM knowledge WHERE business_id=?",(bid,))]

@app.post("/api/businesses/{bid}/knowledge", dependencies=[Depends(authorize)])
def add_knowledge(bid: str, body: KnowledgeInput):
    if MONEY.search(body.answer): raise HTTPException(400,"Price or monetary answers cannot be stored")
    with connect() as db:
        business_or_404(db,bid)
        id=uid()
        db.execute("INSERT INTO knowledge VALUES(?,?,?,?,?)",(id,bid,body.question,body.answer,int(body.approved)))
        db.commit()
    return {"id":id,"approved":body.approved}

@app.post("/api/businesses/{bid}/simulate", dependencies=[Depends(authorize)])
def simulate(bid: str, body: QuestionInput):
    with connect() as db:
        business_or_404(db,bid)
        if PRICE.search(body.question): return {"answer":NO_PRICE,"source":"policy","human_needed":True}
        rows=db.execute("SELECT question,answer FROM knowledge WHERE business_id=? AND approved=1",(bid,)).fetchall()
        for row in rows:
            if row["question"].strip().casefold()==body.question.strip().casefold():
                if MONEY.search(row["answer"]): return {"answer":NO_PRICE,"source":"policy","human_needed":True}
                return {"answer":row["answer"],"source":"approved_knowledge","human_needed":False}
    return {"answer":UNKNOWN,"source":"unknown","human_needed":True}

@app.post("/api/businesses/{bid}/booking-requests", dependencies=[Depends(authorize)])
def booking_request(bid: str, body: BookingInput):
    with connect() as db:
        business_or_404(db,bid)
        id=uid()
        db.execute("INSERT INTO booking_requests VALUES(?,?,?,?,?,?,'requested')",
                   (id,bid,body.name,body.phone,body.service,body.requested_time))
        db.commit()
    return {"id":id,"status":"requested","confirmed":False}

@app.get("/api/businesses/{bid}/voice-preview", dependencies=[Depends(authorize)])
def voice_preview(bid: str):
    with connect() as db:
        b=business_or_404(db,bid)
        faq=db.execute("SELECT question,answer FROM knowledge WHERE business_id=? AND approved=1",(bid,)).fetchall()
        return {"name":b["agent_name"],"firstMessage":b["greeting"],"voice":{"provider":b["voice_provider"],"voiceId":b["voice_id"]},
                "system_prompt":"You are an AI receptionist. Never quote prices or estimates. Never invent information. Escalate unknown questions. Only use approved FAQ facts.",
                "approved_faqs":[dict(x) for x in faq],"not_active":True}

# Cloud persistence is enabled only after server-side Supabase credentials are configured.
from app.cloud import router as cloud_router
app.include_router(cloud_router)

from app.vapi import router as vapi_router
app.include_router(vapi_router)

# Startup diagnostics only log status codes and flags; they never log credentials.
@app.on_event("startup")
def check_integrations_on_boot():
    import logging
    import httpx
    log=logging.getLogger("uvicorn.error")
    db_url=os.getenv("SUPABASE_URL","").rstrip("/")
    db_secret=os.getenv("SUPABASE_SECRET_KEY","")
    vapi_key=os.getenv("VAPI_API_KEY","")
    log.info("callpilot_voice_semantic_webhook ready=%s credential_id=%s webhook_token=%s",
             bool(os.getenv("VAPI_CREDENTIAL_ID") and os.getenv("VAPI_WEBHOOK_TOKEN")),
             bool(os.getenv("VAPI_CREDENTIAL_ID")),bool(os.getenv("VAPI_WEBHOOK_TOKEN")))
    log.info("callpilot_readiness google_client=%s google_secret=%s google_encryption=%s stripe_test_key=%s stripe_webhook_secret=%s stripe_price_id=%s stripe_billing_enabled=%s public_supabase_key=%s",
             bool(os.getenv("GOOGLE_OAUTH_CLIENT_ID")), bool(os.getenv("GOOGLE_OAUTH_CLIENT_SECRET")),
             bool(os.getenv("GOOGLE_TOKEN_ENCRYPTION_KEY")),
             os.getenv("STRIPE_SECRET_KEY","").startswith("sk_test_"),bool(os.getenv("STRIPE_WEBHOOK_SECRET")),
             bool(os.getenv("STRIPE_PRICE_ID")),os.getenv("STRIPE_BILLING_ENABLED")=="true",
             bool(os.getenv("SUPABASE_PUBLISHABLE_KEY")))
    log.info("callpilot_integrations configured supabase_url=%s supabase_secret=%s vapi_key=%s admin_token=%s",
             bool(db_url),bool(db_secret),bool(vapi_key),bool(os.getenv("ADMIN_TOKEN")))
    if db_url and db_secret:
        try:
            with httpx.Client(timeout=5) as c:
                r=c.get(db_url+"/rest/v1/organizations",
                        params={"select":"id","limit":"1"},headers={"apikey":db_secret})
            log.info("callpilot_supabase_probe http_status=%s connected=%s",r.status_code,r.status_code==200)
        except Exception as e:
            log.warning("callpilot_supabase_probe failed error_type=%s",type(e).__name__)
    # One-time semantic rollout: index a small number of pre-existing approved FAQs.
    # Existing indexed facts are skipped on later deployments.
    if db_url and db_secret:
        try:
            from app.cloud import api as cloud_api
            from app.semantic import reindex_approved, retrieve
            orgs=cloud_api("organizations",params={"select":"id","limit":5})
            for organization in orgs:
                result=reindex_approved(organization["id"])
                log.info("callpilot_semantic_backfill indexed=%s",result["indexed"])
                if len(orgs)==1:
                    fact,metadata=retrieve(organization["id"],"Can I buy half container?")
                    log.info("callpilot_semantic_smoke matched=%s reason=%s score=%s",
                             bool(fact),metadata.get("reason"),metadata.get("similarity","n/a"))
                    minimum_fact,minimum_meta=retrieve(organization["id"],"Can I buy 1 product?")
                    log.info("callpilot_minimum_order_smoke matched=%s policy=%s",
                             bool(minimum_fact),minimum_meta.get("reason")=="approved_policy_entailment")
                    unrelated,negative=retrieve(organization["id"],"Do you repair cars and motorcycles?")
                    log.info("callpilot_semantic_negative unknown=%s reason=%s score=%s",
                             unrelated is None,negative.get("reason"),negative.get("similarity","n/a"))
        except Exception as e:
            log.warning("callpilot_semantic_rollout error_type=%s",type(e).__name__)
    # Keep already-provisioned managed assistants on the same global voice policy.
    if db_url and db_secret and vapi_key:
        try:
            from app.cloud import api as cloud_api
            from app.call_intel import refresh_receptionist_prompt
            for org in cloud_api("organizations",params={"select":"id","limit":100}):
                try:
                    outcome=refresh_receptionist_prompt(org["id"])
                    log.info("callpilot_global_voice_policy updated=%s reason=%s",
                             outcome["updated"],outcome["reason"])
                except Exception as exc:
                    log.warning("callpilot_global_voice_policy_failed org=%s error_type=%s",
                                org["id"],type(exc).__name__)
        except Exception as exc:
            log.warning("callpilot_global_voice_policy_rollout_failed error_type=%s",type(exc).__name__)
    # Import initial historical calls once for clearly matched, configured workspaces.
    # A private assistant is linked only when its name uniquely matches the business.
    if db_url and db_secret and vapi_key:
        try:
            from app.call_intel import sync_org_calls
            from app.cloud import api as cloud_api
            for org in cloud_api("organizations",params={"select":"id","limit":5}):
                existing=cloud_api("call_insights",params={
                    "organization_id":"eq."+org["id"],"select":"id","limit":1})
                if not existing:
                    result=sync_org_calls(org["id"],limit=20)
                    log.info("callpilot_call_intelligence_initial_sync imported=%s",result["synced"])
                from app.call_intel import configure_analysis, agent_config
                if agent_config(org["id"]).get("provider_agent_id"):
                    try:
                        from app.usage import discover_linked_business_phone
                        phone_link=discover_linked_business_phone(org["id"])
                        log.info("callpilot_existing_phone_link verified=%s reason=%s",
                                 phone_link.get("linked"),phone_link.get("reason"))
                    except Exception as phone_error:
                        log.warning("callpilot_existing_phone_link failed=%s",type(phone_error).__name__)
                    try:
                        configured=configure_analysis(org["id"])
                        log.info("callpilot_call_analysis_enabled=%s reused=%s",
                                 configured["enabled"],configured.get("existing",False))
                        try:
                            from app.call_intel import disable_knowledge_filler
                            quiet=disable_knowledge_filler(org["id"])
                            log.info("callpilot_voice_filler_fix updated=%s reason=%s",
                                     quiet["updated"],quiet["reason"])
                            from app.call_intel import apply_call_duration_policy
                            cap=apply_call_duration_policy(org["id"])
                            log.info("callpilot_4min_policy configured=%s changed=%s",
                                     cap.get("configured"),cap.get("changed"))
                        except Exception as voice_error:
                            log.warning("callpilot_voice_filler_fix failed=%s",type(voice_error).__name__)
                        # Backfill analysis once on existing calls that predate the new feature.
                        from app.call_intel import request_call_analysis
                        old_calls=cloud_api("call_insights",params={
                            "organization_id":"eq."+org["id"],
                            "analysis_requested_at":"is.null","lead_source":"eq.unreviewed",
                            "select":"provider_call_id","limit":3})
                        for item in old_calls:
                            try:
                                outcome=request_call_analysis(org["id"],item["provider_call_id"])
                                log.info("callpilot_historical_analysis_queued=%s",outcome.get("queued"))
                            except Exception as inner:
                                log.warning("callpilot_historical_analysis_blocked error_type=%s",type(inner).__name__)
                        # Reconcile completed analysis on calls that were queued earlier.
                        # Never log transcript, customer identity, or recording.
                        from app.call_intel import vapi as vapi_api, persist as save_call
                        queued=cloud_api("call_insights",params={
                            "organization_id":"eq."+org["id"],
                            "analysis_requested_at":"not.is.null","lead_source":"eq.unreviewed",
                            "select":"provider_call_id","limit":3})
                        for row in queued:
                            try:
                                detail=vapi_api("GET","/call/"+row["provider_call_id"])
                                artifacts=(detail.get("artifact") or {}).get("structuredOutputs") or {}
                                present=bool(artifacts)
                                log.info("callpilot_queued_analysis_provider_output_available=%s",present)
                                if present:
                                    save_call(org["id"],detail)
                            except Exception as err:
                                log.warning("callpilot_analysis_reconcile_error_type=%s",type(err).__name__)
                    except Exception as e:
                        log.warning("callpilot_call_analysis_blocked error_type=%s",type(e).__name__)
        except Exception as e:
            log.warning("callpilot_call_intelligence_initial_sync blocked=%s",type(e).__name__)
    stripe_key=os.getenv("STRIPE_SECRET_KEY","")
    price_id=os.getenv("STRIPE_PRICE_ID","")
    if stripe_key.startswith("sk_test_") and price_id.startswith("price_"):
        try:
            with httpx.Client(timeout=7) as c:
                resp=c.get("https://api.stripe.com/v1/prices/"+price_id,auth=(stripe_key,""))
            valid=resp.status_code==200
            price=resp.json() if valid else {}
            log.info("callpilot_stripe_price_probe http_status=%s valid=%s active=%s recurring=%s livemode=%s",
                     resp.status_code,valid,bool(price.get("active")),
                     bool(price.get("recurring")),price.get("livemode") if valid else "unknown")
        except Exception as e:
            log.warning("callpilot_stripe_price_probe failed error_type=%s",type(e).__name__)
    if vapi_key:
        try:
            with httpx.Client(timeout=5) as c:
                r=c.get("https://api.vapi.ai/assistant",params={"limit":"1"},headers={"Authorization":"Bearer "+vapi_key})
            log.info("callpilot_vapi_probe http_status=%s authenticated=%s",r.status_code,r.status_code==200)
            # Verify the authenticated recording endpoint without downloading audio
            # or logging private presigned URLs.
            try:
                from app.cloud import api as cloud_api
                recorded=cloud_api("call_insights",params={
                    "recording_url":"not.is.null","select":"provider_call_id","limit":1})
                if recorded:
                    with httpx.Client(timeout=10,follow_redirects=False) as c:
                        response=c.get("https://api.vapi.ai/call/"+
                            recorded[0]["provider_call_id"]+"/mono-recording",
                            headers={"Authorization":"Bearer "+vapi_key})
                    log.info("callpilot_recording_auth_probe http_status=%s redirect_issued=%s",
                             response.status_code,response.status_code in (301,302,303,307,308))
            except Exception as exc:
                log.warning("callpilot_recording_auth_probe error_type=%s",type(exc).__name__)
        except Exception as e:
            log.warning("callpilot_vapi_probe failed error_type=%s",type(e).__name__)

# One-time authorized test-number reassignment. Only move this exact existing
# line from the known Call Pilot assistant to the Metro Print assistant.
@app.on_event("startup")
def metroprint_transfer_test_line_once():
    import logging
    log=logging.getLogger("uvicorn.error")
    old_oid="b550c0af-7d9c-4231-8eec-cb9f6ba9e724"
    new_oid="0e7de94e-d12f-4a9a-96f9-5d3b91588a5a"
    old_assistant="48b785c7-4b05-45f8-af8e-8670510c23e7"
    new_assistant="7eb2a057-c5ff-4243-bed6-37f175cfb695"
    pid="a14f1205-f52e-4617-80b3-050dea2513f8"
    try:
        from app.cloud import api as db
        from app.call_intel import vapi,owns_assistant
        old=db("agent_configs",params={"organization_id":"eq."+old_oid,
            "select":"provider_agent_id,provider_phone_id","limit":1})
        new=db("agent_configs",params={"organization_id":"eq."+new_oid,
            "select":"provider_agent_id,provider_phone_id","limit":1})
        assigned=db("phone_assignments",params={"provider_phone_id":"eq."+pid,
            "select":"organization_id,provider_number","limit":1})
        if (len(old)!=1 or len(new)!=1 or len(assigned)!=1 or
            old[0].get("provider_agent_id")!=old_assistant or
            new[0].get("provider_agent_id")!=new_assistant or
            assigned[0].get("provider_number")!="+13513453081" or
            new[0].get("provider_phone_id") not in (None,pid)):
            log.warning("metroprint_transfer result=blocked_changed_database")
            return
        if assigned[0].get("organization_id")==new_oid:
            log.info("metroprint_transfer result=already_linked_in_database")
            return
        if (assigned[0].get("organization_id")!=old_oid or
            old[0].get("provider_phone_id")!=pid):
            log.warning("metroprint_transfer result=blocked_source_not_owner")
            return
        target=vapi("GET","/assistant/"+new_assistant)
        source=vapi("GET","/assistant/"+old_assistant)
        if not owns_assistant(target,new_oid) or not owns_assistant(source,old_oid):
            log.warning("metroprint_transfer result=blocked_assistant_identity")
            return
        phone=vapi("GET","/phone-number/"+pid)
        if (phone.get("id")!=pid or phone.get("number")!="+13513453081" or
            phone.get("assistantId") not in (old_assistant,new_assistant) or
            phone.get("server") or phone.get("squadId")):
            log.warning("metroprint_transfer result=blocked_unexpected_vapi_routing")
            return
        if phone.get("assistantId")==old_assistant:
            vapi("PATCH","/phone-number/"+pid,body={"assistantId":new_assistant})
        verified=vapi("GET","/phone-number/"+pid)
        if verified.get("assistantId")!=new_assistant:
            log.warning("metroprint_transfer result=provider_verification_failed")
            return
        # Database reconciliation is performed separately as one checked SQL
        # transaction, avoiding partially moved ownership between tenant rows.
        log.info("metroprint_transfer result=provider_routed_to_metroprint")
    except Exception as exc:
        log.warning("metroprint_transfer result=failed error_type=%s detail=%s",
                    type(exc).__name__,str(getattr(exc,"detail","unavailable"))[:110])

from app.account import router as account_router
from app.billing import router as billing_router
app.include_router(account_router)
app.include_router(billing_router)

@app.get('/app',response_class=HTMLResponse)
def customer_app():
    return (ROOT/'static'/'customer.html').read_text()

@app.get('/legal',response_class=HTMLResponse)
def legal_page():
    return (ROOT/'static'/'legal.html').read_text()

@app.get('/operator',response_class=HTMLResponse)
def operator_app():
    return (ROOT/'static'/'cloud.html').read_text()

from app.call_intel import router as call_intel_router
app.include_router(call_intel_router)

from app.calendar import router as calendar_router
app.include_router(calendar_router)

from app.usage import router as usage_router
app.include_router(usage_router)
