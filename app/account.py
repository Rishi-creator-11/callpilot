from app.semantic import index_fact, reindex_approved, retrieve, enabled
"""Customer self-service API: verified Supabase identity and tenant membership."""
import hashlib, os, uuid, httpx
from fastapi import APIRouter, Header, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal
from app.cloud import api, PRICE, MONEY, NO_PRICE, UNKNOWN, NOT_CARRIED
from app.website_import import fetch_page
from app.pdf_import import extract_pdf, MAX_PDF_BYTES
from app import places
from starlette.concurrency import run_in_threadpool

router=APIRouter(prefix="/api/account",tags=["customer"])
# Only the owner's Call Pilot marketing workspace may publish its fixed plans.
# Never allow other tenants to bypass price protections.
CALLPILOT_ORG="b550c0af-7d9c-4231-8eec-cb9f6ba9e724"
PLAN_QUESTION="What are CallPilot's plans?"
PLAN_ANSWER=(
    "Starter includes 100 AI calling minutes per month for $49 per month. "
    "Growth includes 300 AI calling minutes per month for $99 per month. "
    "Business includes 600 AI calling minutes per month for $179 per month. "
    "Every plan includes an AI receptionist, approved business knowledge management, "
    "call history, available recordings and transcripts, AI call summaries, lead insights, "
    "and the business dashboard. Each plan has a $25 one-time setup fee covering initial "
    "business setup, knowledge configuration, phone connection assistance, and launch testing. "
    "Calls are capped at four minutes each. There are no automatic overage charges. "
    "Applicable taxes are additional. Published prices are fixed; no discounts or custom deals."
)
def allowed_plan_fact(oid,question,answer):
    return oid==CALLPILOT_ORG and question.strip()==PLAN_QUESTION and answer.strip()==PLAN_ANSWER
def tenant_prices_enabled(oid):
    if oid==CALLPILOT_ORG:return True
    rows=api("agent_configs",params={"organization_id":"eq."+oid,
        "select":"allow_price_quotes","limit":1})
    return bool(rows and rows[0].get("allow_price_quotes"))

def validate_fact(oid,question,answer):
    if (PRICE.search(question) or MONEY.search(answer)) and not (
        allowed_plan_fact(oid,question,answer)
        or (oid!=CALLPILOT_ORG and tenant_prices_enabled(oid))):
        raise HTTPException(400,"Enable verified business pricing in Launch Studio before publishing exact prices.")

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
    call_instructions:str|None=Field(default=None,max_length=2400)

@router.get("/config")
def config():
    return {"supabase_url":SUPA() if PUB() else None,"supabase_publishable_key":PUB() or None,
            "billing_enabled":False,"signup_enabled":bool(PUB()),"business_lookup_enabled":places.configured()}


class BusinessLookup(BaseModel):
    query: str = Field(min_length=3, max_length=180)


@router.post("/business-lookup")
def business_lookup(body: BusinessLookup, response: Response, user=Depends(current_user)):
    query = " ".join(body.query.split())
    if len(query) < 3:
        raise HTTPException(422, "Enter a business name and city, or a business phone number.")
    response.headers["Cache-Control"] = "no-store"
    return {"places": places.search_businesses(query, user["id"])}

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
    a=api("agent_configs",params={"organization_id":"eq."+oid,"select":"agent_name,greeting,call_instructions,voice_provider,voice_id,voice_consent_verified_at"})
    return a[0] if a else {}

@router.put("/organizations/{oid}/agent")
def update_agent(oid:str,body:AgentEdit,user=Depends(current_user)):
    membership(oid,user,("owner","admin"))
    save_agent_configuration(oid,body)
    return {"saved":True}


def save_agent_configuration(oid:str,body:AgentEdit):
    saved=rpc("save_agent_configuration",{
        "p_organization_id":oid,"p_agent_name":body.agent_name,"p_greeting":body.greeting,
        "p_call_instructions":body.call_instructions,
    })
    if saved is not True:raise HTTPException(404,"No receptionist is configured for this workspace")

@router.get("/organizations/{oid}/knowledge")
def list_knowledge(oid:str,user=Depends(current_user)):
    membership(oid,user)
    return api("knowledge_facts",params={"organization_id":"eq."+oid,
        "source_document_id":"is.null","select":"id,question,answer,approved_at,created_at","order":"created_at.desc"})

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


class WebsiteImport(BaseModel):
    url: str = Field(min_length=3, max_length=2048)


class BusinessDescription(BaseModel):
    title:str=Field(min_length=3,max_length=120)
    content:str=Field(min_length=20,max_length=12000)

def _description_policy(oid,content):
    # An owner may describe shipping rates, payment policies or pricing procedures
    # in ordinary language. Numeric price quotes remain restricted under the
    # platform's existing tenant policy; the voice tool also blocks price asks.
    if MONEY.search(content):
        raise HTTPException(400,"Remove monetary amounts from the description; published prices require a separately approved pricing rule.")

@router.get("/organizations/{oid}/descriptions")
def list_descriptions(oid:str,user=Depends(current_user)):
    membership(oid,user)
    return api("knowledge_documents",params={"organization_id":"eq."+oid,
        "select":"id,title,content,source_url,source_filename,approved_at,created_at","order":"created_at.desc","limit":100})

@router.post("/organizations/{oid}/descriptions")
def create_description(oid:str,body:BusinessDescription,user=Depends(current_user)):
    membership(oid,user,("owner","admin"))
    title,content=body.title.strip(),body.content.strip()
    _description_policy(oid,content)
    rows=api("knowledge_documents","POST",data={"organization_id":oid,"title":title,
        "content":content},prefer="return=representation")
    return {"id":rows[0]["id"],"approved":False}

@router.post("/organizations/{oid}/descriptions/import-website/preview")
async def preview_website(oid: str, body: WebsiteImport, user=Depends(current_user)):
    """Read the full website for review; never mutate knowledge or publish prices.

    This is deliberately an authenticated, workspace-scoped preview rather than an
    anonymous demo. An owner may inspect imported prices before approving them.
    """
    await run_in_threadpool(membership, oid, user, ("owner", "admin"))
    from app.site_reader import read_site
    result = await run_in_threadpool(read_site, body.url)
    return {
        "source_url": result["source_url"],
        "title": result["title"],
        "description": result["description"],
        "stats": result["stats"],
        "products": result["products"],
        "pages": result["pages"],
        "phone": result["phone"],
        "email": result["email"],
        "hours": result["hours"],
        "policies": result["policies"],
        "faqs": result["faqs"],
        "requires_review": True,
        "saved": False,
    }


@router.post("/organizations/{oid}/descriptions/import-website")
def import_website(oid: str, body: WebsiteImport, user=Depends(current_user)):
    membership(oid, user, ("owner", "admin"))
    page = fetch_page(body.url)
    rows = api(
        "knowledge_documents", "POST",
        params={"on_conflict": "organization_id,source_url"},
        data={"organization_id": oid, **page},
        prefer="resolution=ignore-duplicates,return=representation",
    )
    return {"created": bool(rows), "source_url": page["source_url"]}


@router.post("/organizations/{oid}/descriptions/import-pdf")
async def import_pdf(oid: str, request: Request,
                     filename: str = Query(min_length=5, max_length=200),
                     user=Depends(current_user)):
    await run_in_threadpool(membership, oid, user, ("owner", "admin"))
    name = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not name.lower().endswith(".pdf") or not name[:-4].strip() or not name.isprintable():
        raise HTTPException(422, "Choose a PDF file with a valid filename.")
    if request.headers.get("content-type") != "application/pdf":
        raise HTTPException(415, "Upload a PDF document.")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_PDF_BYTES:
            raise HTTPException(413, "PDF files must be 5 MB or smaller.")
        body.extend(chunk)
    content = await run_in_threadpool(extract_pdf, bytes(body))
    rows = await run_in_threadpool(
        api, "knowledge_documents", "POST",
        params={"on_conflict": "organization_id,source_sha256"},
        data={"organization_id": oid, "title": name[:120], "content": content,
              "source_filename": name, "source_sha256": hashlib.sha256(body).hexdigest()},
        prefer="resolution=ignore-duplicates,return=representation",
    )
    return {"created": bool(rows)}


@router.put("/organizations/{oid}/descriptions/{did}")
def edit_description(oid:str,did:str,body:BusinessDescription,user=Depends(current_user)):
    membership(oid,user,("owner","admin"))
    rows=api("knowledge_documents",params={"organization_id":"eq."+oid,"id":"eq."+did,"select":"id"})
    if not rows:raise HTTPException(404,"Description not found")
    title,content=body.title.strip(),body.content.strip()
    _description_policy(oid,content)
    # Unpublish stale searchable passages before changing source text.
    api("knowledge_documents","PATCH",params={"organization_id":"eq."+oid,"id":"eq."+did},
        data={"approved_at":None,"approved_by":None},prefer="return=minimal")
    api("knowledge_facts","DELETE",params={"organization_id":"eq."+oid,"source_document_id":"eq."+did},
        prefer="return=minimal")
    api("knowledge_documents","PATCH",params={"organization_id":"eq."+oid,"id":"eq."+did},
        data={"title":title,"content":content,
              "updated_at":__import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()},
        prefer="return=minimal")
    return {"saved":True,"approved":False,"requires_reapproval":True}

@router.delete("/organizations/{oid}/descriptions/{did}")
def delete_description(oid:str,did:str,user=Depends(current_user)):
    membership(oid,user,("owner","admin"))
    rows=api("knowledge_documents",params={"organization_id":"eq."+oid,"id":"eq."+did,"select":"id"})
    if not rows:raise HTTPException(404,"Description not found")
    # Children disappear with FK ON DELETE CASCADE.
    api("knowledge_documents","DELETE",params={"organization_id":"eq."+oid,"id":"eq."+did},
        prefer="return=minimal")
    return {"deleted":True}

@router.post("/organizations/{oid}/descriptions/{did}/approve")
def approve_description(oid:str,did:str,user=Depends(current_user)):
    membership(oid,user,("owner","admin"))
    doc=api("knowledge_documents",params={"organization_id":"eq."+oid,
        "id":"eq."+did,"select":"id,title,content,approved_at"})
    if not doc:raise HTTPException(404,"Description not found")
    doc=doc[0]
    _description_policy(oid,doc["content"])
    from app.semantic import embeddings,to_pgvector,MODEL,split_description
    from datetime import datetime,timezone
    sections=split_description(doc["content"])
    if not sections:raise HTTPException(422,"No searchable business information found")
    # Embedding must succeed for all passages before any approval is published.
    vectors=embeddings([doc["title"]+": "+part for part in sections])
    api("knowledge_facts","DELETE",params={"organization_id":"eq."+oid,
        "source_document_id":"eq."+did},prefer="return=minimal")
    now=datetime.now(timezone.utc).isoformat()
    rows=[{"organization_id":oid,"source_document_id":did,
           "question":"Business description — "+doc["title"]+": "+part,"answer":part,
           "question_embedding_384":to_pgvector(vector),
           "embedding_model":MODEL,"embedded_at":now}
          for part,vector in zip(sections,vectors)]
    try:
        api("knowledge_facts","POST",data=rows,prefer="return=minimal")
        # Publish all passages only after every insert succeeded.
        api("knowledge_facts","PATCH",params={"organization_id":"eq."+oid,
            "source_document_id":"eq."+did},data={"approved_at":now,
            "approved_by":user["id"]},prefer="return=minimal")
        api("knowledge_documents","PATCH",params={"organization_id":"eq."+oid,
            "id":"eq."+did},data={"approved_at":now,"approved_by":user["id"]},
            prefer="return=minimal")
    except Exception:
        # Failed publication cannot leave partially approved knowledge visible.
        api("knowledge_facts","DELETE",params={"organization_id":"eq."+oid,
            "source_document_id":"eq."+did},prefer="return=minimal")
        raise
    return {"approved":True,"indexed_passages":len(rows)}

@router.post("/organizations/{oid}/simulate")
def simulate(oid:str,body:Question,user=Depends(current_user)):
    membership(oid,user)
    return answer_question(oid, body.question)


def answer_question(oid: str, question: str):
    if PRICE.search(question) or (oid==CALLPILOT_ORG and "plan" in question.casefold()):
        if oid==CALLPILOT_ORG:
            import re
            if re.search(r"\b(discount|negotiate|deal|waive|reduce|lower|free trial|custom price)\b",question,re.I):
                return {"answer":"Published prices are fixed. Our team can discuss requirements, but I cannot negotiate or offer deals.","source":"policy"}
            return {"answer":PLAN_ANSWER,"source":"published_pricing"}
        if not tenant_prices_enabled(oid):return {"answer":NO_PRICE,"source":"policy"}
    # Lazy backfill for FAQs approved before embeddings were introduced.
    pending=api("knowledge_facts",params={"organization_id":"eq."+oid,
        "approved_at":"not.is.null","question_embedding_384":"is.null","select":"id","limit":1})
    if pending:
        reindex_approved(oid)
    fact,metadata=retrieve(oid,question)
    if fact:
        if MONEY.search(fact["answer"]) and not (allowed_plan_fact(oid,fact["question"],fact["answer"]) or (oid!=CALLPILOT_ORG and tenant_prices_enabled(oid))):
            return {"answer":NO_PRICE,"source":"policy"}
        return {"answer":fact["answer"],"source":"approved_semantic",
                "matched_question":fact["question"],"similarity":metadata["similarity"]}
    return {"answer":NOT_CARRIED if metadata["reason"]=="item_not_listed" else UNKNOWN,"source":"unknown","reason":metadata["reason"]}

class KnowledgeCheck(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    question: str = Field(min_length=3, max_length=500)
    expectation: Literal["contains", "unknown", "policy"]
    expected_text: str = Field(default="", max_length=300)


CHECK_FIELDS = "id,question,expectation,expected_text"


@router.get("/organizations/{oid}/knowledge-checks")
def list_knowledge_checks(oid: uuid.UUID, user=Depends(current_user)):
    membership(str(oid), user)
    return api("knowledge_checks", params={"organization_id": f"eq.{oid}",
                                           "select": CHECK_FIELDS, "order": "created_at.asc,id.asc"})


@router.post("/organizations/{oid}/knowledge-checks", status_code=201)
def create_knowledge_check(oid: uuid.UUID, body: KnowledgeCheck, user=Depends(current_user)):
    membership(str(oid), user, ("owner", "admin"))
    if body.expectation == "contains" and not body.expected_text:
        raise HTTPException(422, "Enter the phrase you expect in the answer.")
    return api("knowledge_checks", "POST", params={"select": CHECK_FIELDS},
               data={"organization_id": str(oid), "question": body.question,
                     "expectation": body.expectation,
                     "expected_text": body.expected_text if body.expectation == "contains" else ""},
               prefer="return=representation")[0]


@router.delete("/organizations/{oid}/knowledge-checks/{check_id}", status_code=204)
def delete_knowledge_check(oid: uuid.UUID, check_id: uuid.UUID, user=Depends(current_user)):
    membership(str(oid), user, ("owner", "admin"))
    api("knowledge_checks", "DELETE", params={"organization_id": f"eq.{oid}", "id": f"eq.{check_id}"})


@router.post("/organizations/{oid}/knowledge-checks/{check_id}/run")
def run_knowledge_check(oid: uuid.UUID, check_id: uuid.UUID, user=Depends(current_user)):
    membership(str(oid), user)
    rows = api("knowledge_checks", params={"organization_id": f"eq.{oid}",
                                          "id": f"eq.{check_id}", "select": CHECK_FIELDS})
    if not rows:
        raise HTTPException(404, "Knowledge check not found.")
    check = rows[0]
    result = answer_question(str(oid), check["question"])
    if check["expectation"] == "contains":
        expected = " ".join(check["expected_text"].casefold().split())
        answer = " ".join(result["answer"].casefold().split())
        passed = result["source"] in {"approved_semantic", "published_pricing"} and expected in answer
    else:
        passed = result["source"] == check["expectation"]
    return {**result, "passed": passed}


class PricingCapabilities(BaseModel):
    enabled:bool
    owner_confirms_prices_are_verified:bool=False

@router.get("/organizations/{oid}/verified-pricing")
def verified_pricing(oid:str,user=Depends(current_user)):
    membership(oid,user)
    rows=api("agent_configs",params={"organization_id":"eq."+oid,
        "select":"allow_price_quotes,price_quote_approved_at","limit":1})
    cfg=rows[0] if rows else {}
    return {"enabled":bool(cfg.get("allow_price_quotes")),
            "approved_at":cfg.get("price_quote_approved_at")}

@router.put("/organizations/{oid}/verified-pricing")
def set_verified_pricing(oid:str,body:PricingCapabilities,user=Depends(current_user)):
    membership(oid,user,("owner","admin"))
    if oid==CALLPILOT_ORG:
        raise HTTPException(409,"CallPilot's own published plan prices are managed centrally")
    if body.enabled and body.owner_confirms_prices_are_verified is not True:
        raise HTTPException(422,"Confirm that only current owner-approved prices will be published")
    from datetime import datetime,timezone
    original=api("agent_configs",params={"organization_id":"eq."+oid,
        "select":"allow_price_quotes,price_quote_approved_at,price_quote_approved_by","limit":1})
    if not original:raise HTTPException(404,"Business assistant configuration missing")
    updates={"allow_price_quotes":body.enabled,
       "price_quote_approved_at":datetime.now(timezone.utc).isoformat() if body.enabled else None,
       "price_quote_approved_by":user["id"] if body.enabled else None}
    api("agent_configs","PATCH",params={"organization_id":"eq."+oid},
        data=updates,prefer="return=minimal")
    try:
        from app.call_intel import refresh_receptionist_prompt
        result=refresh_receptionist_prompt(oid)
        if result.get("reason") not in ("already_current","voice_policy_and_one_second_opening_synced","no_assistant"):
            raise HTTPException(502,"Could not synchronize pricing policy with assistant")
    except Exception:
        api("agent_configs","PATCH",params={"organization_id":"eq."+oid},
            data={k:original[0][k] for k in updates},prefer="return=minimal")
        raise
    return {"enabled":body.enabled,
            "note":"Only matching separately approved exact price facts may be spoken."}

class VoiceCapabilities(BaseModel):
    leads_enabled:bool=False
    booking_enabled:bool=False
    catalog_enabled:bool=False
    order_requests_enabled:bool=False

@router.get("/organizations/{oid}/voice-capabilities")
def voice_capabilities(oid:str,user=Depends(current_user)):
    """Only explicitly approved capabilities are exposed to the AI."""
    membership(oid,user)
    rows=api("agent_configs",params={"organization_id":"eq."+oid,
        "select":"provider_agent_id,voice_leads_enabled,voice_booking_enabled,voice_catalog_enabled,voice_order_requests_enabled","limit":1})
    if not rows:raise HTTPException(404,"Assistant configuration not found")
    from app.calendar import connection
    cfg=rows[0]
    return {"leads_enabled":bool(cfg.get("voice_leads_enabled")),
        "booking_enabled":bool(cfg.get("voice_booking_enabled")),
        "catalog_enabled":bool(cfg.get("voice_catalog_enabled")),
        "order_requests_enabled":bool(cfg.get("voice_order_requests_enabled")),
        "calendar_connected":bool(connection(oid)),
        "assistant_connected":bool(cfg.get("provider_agent_id")),
        "voice_booking_configured":bool(cfg.get("voice_booking_enabled") and connection(oid)
                                   and cfg.get("provider_agent_id")),
        "live_call_tested":False}

@router.put("/organizations/{oid}/voice-capabilities")
def set_voice_capabilities(oid:str,body:VoiceCapabilities,user=Depends(current_user)):
    """Owner approval, per-tenant ownership verification, provider synchronization."""
    membership(oid,user,("owner","admin"))
    from app.calendar import connection
    from app.call_intel import refresh_receptionist_prompt,owns_assistant,vapi
    keys=("voice_leads_enabled","voice_booking_enabled",
          "voice_catalog_enabled","voice_order_requests_enabled")
    rows=api("agent_configs",params={"organization_id":"eq."+oid,
        "select":"provider_agent_id,"+",".join(keys),"limit":1})
    if not rows or not rows[0].get("provider_agent_id"):
        raise HTTPException(409,"Connect a managed Vapi assistant before enabling voice actions")
    aid=rows[0]["provider_agent_id"]
    assistant=vapi("GET","/assistant/"+aid)
    if not owns_assistant(assistant,oid):
        raise HTTPException(409,"This assistant is not owned by the selected workspace")
    if body.booking_enabled and not connection(oid):
        raise HTTPException(409,"Connect your business Google Calendar before enabling voice booking")
    if body.order_requests_enabled and not body.catalog_enabled:
        raise HTTPException(422,"Enable approved catalog lookup before accepting order requests")
    if body.order_requests_enabled:
        approved=api("catalog_items",params={"organization_id":"eq."+oid,
            "active":"eq.true","approved_at":"not.is.null","select":"id","limit":1})
        if not approved:raise HTTPException(409,"Approve at least one product before enabling order requests")
    old=rows[0]
    updates=dict(zip(keys,(body.leads_enabled,body.booking_enabled,
                            body.catalog_enabled,body.order_requests_enabled)))
    api("agent_configs","PATCH",params={"organization_id":"eq."+oid},
        data=updates,prefer="return=minimal")
    try:
        result=refresh_receptionist_prompt(oid)
        if result.get("reason") not in ("already_current","voice_policy_and_one_second_opening_synced"):
            raise HTTPException(502,"Could not verify synchronization with your voice assistant")
    except Exception:
        api("agent_configs","PATCH",params={"organization_id":"eq."+oid},
            data={key:old[key] for key in keys},prefer="return=minimal")
        raise
    return {"saved":True,"provider_synced":True,"leads_enabled":body.leads_enabled,
            "booking_enabled":body.booking_enabled,
            "catalog_enabled":body.catalog_enabled,
            "order_requests_enabled":body.order_requests_enabled}

@router.get("/organizations/{oid}/bookings")
def bookings(oid:str,user=Depends(current_user)):
    membership(oid,user)
    rows=api("appointment_requests",params={"organization_id":"eq."+oid,
        "select":"id,caller_name,service,requested_time,duration_minutes,status,created_at,calendar_event_id,idempotency_key,move_id,move_start,move_minutes,cancel_pending_ref",
        "order":"created_at.desc","limit":50})
    for row in rows:
        event=row.pop("calendar_event_id",None)
        reservation=row.pop("idempotency_key",None)
        row["calendar_managed"]=bool(event and reservation)
        row["cancel_pending"]=bool(row.pop("cancel_pending_ref",None))
        if not row["move_start"]:
            row.pop("move_id", None)
    return rows

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
            "note":"Existing members are shown without exposing other users' private contact details."}
