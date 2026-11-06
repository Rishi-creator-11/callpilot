"""CallPilot protected internal demo; no live booking or telephony is activated."""
from __future__ import annotations
import os, sqlite3, uuid, hmac, re
from pathlib import Path
from datetime import datetime, timezone
from fastapi import FastAPI, Header, HTTPException, Depends
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

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

def authorize(x_admin_token: str | None = Header(default=None)):
    expected = os.getenv("ADMIN_TOKEN", "")
    if not expected or not x_admin_token or not hmac.compare_digest(expected, x_admin_token):
        raise HTTPException(401, "Admin token required")

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

PRICE = re.compile(r"\b(price|prices|pricing|cost|costs|fee|fees|quote|estimate|charge|charges|rate|rates|discount|how much|ballpark|dollars?|usd)\b|\$", re.I)
MONEY = re.compile(r"\$\s*\d|\b\d+(?:\.\d+)?\s*(?:dollars?|usd|bucks)\b", re.I)
NO_PRICE = "I can't quote prices or estimates. I can arrange for a team member to follow up."
UNKNOWN = "I don't have verified information about that. I can ask a team member to follow up."

@app.get("/health")
def health(): return {"status":"ok","mode":"internal-demo"}

@app.get("/", response_class=HTMLResponse)
def home():
    return (ROOT/"static"/"index.html").read_text()

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
