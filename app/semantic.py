"""CallPilot semantic FAQ retrieval: real model embeddings + Supabase pgvector.

Answers are copied from approved business facts, never composed by a language model.
A missing embedding provider is an explicit setup error, not an invented answer.
"""
import os
from datetime import datetime,timezone
import httpx
from fastapi import HTTPException
from app.cloud import api

MODEL="gte-small"
DIMENSIONS=384
# Provisional conservative limits. Calibrate on real positive and negative questions.
MIN_SIMILARITY=0.76
MIN_MARGIN=0.035

def enabled():
    return bool(os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SECRET_KEY"))

def embeddings(texts:list[str])->list[list[float]]:
    if not enabled():
        raise HTTPException(503,"Semantic search requires a Supabase connection")
    if not texts or any(not v.strip() or len(v)>3000 for v in texts):
        raise HTTPException(422,"Invalid text for embedding")
    url=os.environ["SUPABASE_URL"].rstrip("/")+"/functions/v1/callpilot-embed"
    secret=os.environ["SUPABASE_SECRET_KEY"]
    vectors=[]
    try:
        with httpx.Client(timeout=40) as c:
            for value in texts:
                r=c.post(url,headers={"x-callpilot-internal-key":secret,
                                      "Content-Type":"application/json"},
                         json={"input":value})
                if r.status_code!=200:
                    raise HTTPException(503,"Supabase embedding service unavailable (HTTP "+str(r.status_code)+")")
                vector=r.json().get("embedding",[])
                if len(vector)!=DIMENSIONS:
                    raise HTTPException(502,"Embedding response had an unexpected size")
                vectors.append(vector)
    except httpx.RequestError:
        raise HTTPException(503,"Embedding service unavailable")
    return vectors

def to_pgvector(vector:list[float])->str:
    return "["+",".join(format(v,".9g") for v in vector)+"]"

def index_fact(organization_id:str,fact_id:str,question:str,approved_by:str|None=None):
    v=embeddings([question])[0]
    data={"question_embedding_384":to_pgvector(v),"embedding_model":MODEL,
          "embedded_at":datetime.now(timezone.utc).isoformat()}
    if approved_by:
        data.update({"approved_at":datetime.now(timezone.utc).isoformat(),"approved_by":approved_by})
    api("knowledge_facts","PATCH",
        params={"organization_id":"eq."+organization_id,"id":"eq."+fact_id},
        data=data,prefer="return=minimal")

def reindex_approved(organization_id:str)->dict:
    facts=api("knowledge_facts",params={"organization_id":"eq."+organization_id,
        "approved_at":"not.is.null","question_embedding_384":"is.null",
        "select":"id,question","limit":100})
    if not facts:return {"indexed":0,"remaining":0}
    vectors=embeddings([row["question"] for row in facts])
    for row,vector in zip(facts,vectors):
        api("knowledge_facts","PATCH",params={"organization_id":"eq."+organization_id,"id":"eq."+row["id"]},
            data={"question_embedding_384":to_pgvector(vector),
                  "embedding_model":MODEL,"embedded_at":datetime.now(timezone.utc).isoformat()},
            prefer="return=minimal")
    return {"indexed":len(facts),"remaining":"run again if more than 100 approved facts"}


def infer_container_minimum(question:str, approved_facts:list[dict]):
    """Use only explicit, approved full-container minimum-order policy."""
    import re
    q=question.casefold()
    small=bool(re.search(r"\b(?:one|1|single|individual|half|partial|one piece|one item|one product|few items|one unit)\b",q))
    goods=bool(re.search(r"\b(?:product|item|piece|unit|container|garment|clothing|shirt|jacket|pants|order)\b",q))
    buy=bool(re.search(r"\b(?:buy|purchase|order|get|sell|available|can i|can we|do you)\b",q))
    if not(small and goods and buy):
        return None
    matched=[]
    for fact in approved_facts:
        a=fact["answer"].casefold()
        if ("only" in a and re.search(r"\bfull containers?\b",a)
                and re.search(r"\b(?:sell|do|offer|ship|accept)\b",a)):
            matched.append(fact)
    return matched[0] if len(matched)==1 else None

def _approved_policy_fallback(organization_id:str,question:str,reason:str):
    facts=api("knowledge_facts",params={"organization_id":"eq."+organization_id,
        "approved_at":"not.is.null","select":"question,answer","limit":100})
    match=infer_container_minimum(question,facts)
    if match:
        return match,{"reason":"approved_policy_entailment","similarity":None}
    return None,{"reason":reason}

def retrieve(organization_id:str,question:str):
    vector=embeddings([question])[0]
    secret=os.getenv("SUPABASE_SECRET_KEY","")
    if not secret:raise HTTPException(503,"Database secret not configured")
    base=os.getenv("SUPABASE_URL","").rstrip("/")
    try:
        with httpx.Client(timeout=12) as c:
            response=c.post(base+"/rest/v1/rpc/match_callpilot_knowledge",
                headers={"apikey":secret,"Content-Type":"application/json"},
                json={"p_org_id":organization_id,"p_embedding":to_pgvector(vector),"p_limit":3})
    except httpx.RequestError:raise HTTPException(503,"Semantic database search unavailable")
    if response.status_code!=200:
        raise HTTPException(503,"Semantic database search failed (HTTP "+str(response.status_code)+")")
    rows=response.json()
    if not rows:return _approved_policy_fallback(organization_id,question,"no_indexed_facts")
    best=rows[0]
    score=float(best["similarity"])
    runner_up=float(rows[1]["similarity"]) if len(rows)>1 else -1
    if score<MIN_SIMILARITY:return _approved_policy_fallback(organization_id,question,"below_confidence_threshold")
    if len(rows)>1 and score-runner_up<MIN_MARGIN:
        return _approved_policy_fallback(organization_id,question,"ambiguous_retrieval")
    return best,{"reason":"approved_semantic_match","similarity":round(score,3)}
