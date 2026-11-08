"""CallPilot semantic FAQ retrieval: real model embeddings + Supabase pgvector.

Answers are copied from approved business facts, never composed by a language model.
A missing embedding provider is an explicit setup error, not an invented answer.
"""
import os,re,difflib
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

def split_description(content:str)->list[str]:
    """Divide owner-written prose into short meaning-preserving passages.

    Preserve paragraph boundaries, avoid cutting words, and keep each passage
    short enough for the embedding service and conversational retrieval.
    """
    paragraphs=[p.strip() for p in re.split(r"\n\s*\n+",content) if p.strip()]
    pieces=[]
    for paragraph in paragraphs:
        sentences=re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])",paragraph)
        current=""
        for sentence in sentences:
            if len(sentence)>700:
                if current:
                    pieces.append(current)
                    current=""
                # Split oversized product lists while preserving word boundaries.
                words=sentence.split()
                for word in words:
                    if len(word)>700:
                        if current:pieces.append(current);current=""
                        pieces.extend(word[i:i+700] for i in range(0,len(word),700))
                    elif len(current)+len(word)+1>700:
                        pieces.append(current)
                        current=word
                    else:
                        current=(current+" "+word).strip()
                continue
            if len(current)+len(sentence)+1>700 and current:
                pieces.append(current)
                current=sentence
            else:
                current=(current+" "+sentence).strip()
        if current:pieces.append(current)
    # Extremely short adjacent paragraphs (e.g. short list items) can be indexed
    # independently; never combine conflicting headings into one answer.
    if len(pieces)>25:
        raise HTTPException(422,"Description is too long. Split it into smaller documents.")
    return [p for p in pieces if p]

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
    goods=bool(re.search(r"\b(?:products?|items?|pieces?|units?|containers?|garments?|clothing|shirts?|jackets?|pants|orders?)\b",q))
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

# A hybrid relevance check combines semantic meaning with lexical/intent evidence.
# GTE-small can assign high cosine scores to unrelated short questions, so avoid
# blindly returning a top vector hit or rejecting a near-tie on cosine alone.
_STOP=frozenset(("a an the i me my you your we our he she it its they them their "
    "is are was were do does did can could would should have has had be been to for "
    "of on in at from with about what which who how why when where please tell "
    "know information more want wanna need get give and or if this that these those "
    "there here some any all much many also please").split())
_SYNONYMS={"subscriptions":"plan","subscription":"plan","plans":"plan","packages":"plan",
           "booking":"appointment","bookings":"appointment","schedule":"appointment",
           "scheduling":"appointment","meetings":"appointment",
           "setup":"start","configure":"start","configured":"start","onboarding":"start",
           "functions":"feature","function":"feature","capabilities":"feature",
           "services":"service","offerings":"service","offers":"service",
           "recordings":"recording","calls":"call","customers":"customer",
           "appointments":"appointment","prices":"price","pricing":"price",
           "delivery":"shipping","deliver":"shipping","deliveries":"shipping",
           "shipped":"shipping","ships":"shipping","shipping":"shipping",
           "sell":"product","sells":"product","selling":"product","products":"product",
           "returns":"return","refunds":"refund",
           "ship":"shipping","ships":"shipping","shipped":"shipping",
           "close":"hours","closing":"hours","closed":"hours",
           "opening":"hours","open":"hours","hours":"hours",
           "carry":"product","carried":"product","stock":"product",
           "selling":"product","sells":"product","sell":"product"}
def _terms(text:str)->set[str]:
    tokens=re.findall(r"[a-z0-9]+",text.casefold())
    return {_SYNONYMS.get(w,w[:-1] if w.endswith("s") and len(w)>5 else w)
            for w in tokens if w not in _STOP and len(w)>1}
def _intent(text:str)->str|None:
    text=text.casefold()
    if re.search(r"\b(what is|who are|tell me (more |a bit |something )?about|know more about|information about)\b",text):
        return "overview"
    if re.search(r"\b(what (can|does|do)|how (can|does|do).*(help|work)|features?|capabilities?|what.*(provide|offer))\b",text):
        return "capabilities"
    if re.search(r"\b(how.*(start|sign up|set up|register|begin)|getting started)\b",text):
        return "onboarding"
    return None
def _question_intent(text:str)->str|None:
    t=text.casefold()
    if re.search(r"\b(what is|who are)\b",t):return "overview"
    if re.search(r"\b(what (can|does|do)|features?|capabilities?)\b",t):return "capabilities"
    if re.search(r"\b(how.*(start|sign up|set up|register|begin))\b",t):return "onboarding"
    return None
def _normalize_spoken_name(oid:str,question:str)->tuple[str,set[str]]:
    org=api("organizations",params={"id":"eq."+oid,"select":"name","limit":1})
    name=(org[0].get("name") if org else "") or ""
    brand=re.sub(r"[^a-z0-9]","",name.casefold())
    brands=_terms(name)|_terms(brand)
    # Resolve close single-token ASR distortions only when the name in the
    # current workspace is an unambiguous candidate, e.g. Copilot -> CallPilot.
    if 6<=len(brand)<=20:
        tokens=re.findall(r"\b[a-zA-Z][a-zA-Z0-9]{4,}\b",question)
        for token in tokens:
            candidate=token.casefold()
            if (candidate!=brand and len(candidate)>=6 and
                abs(len(candidate)-len(brand))<=2 and
                candidate[0]==brand[0] and candidate[-1]==brand[-1] and
                difflib.SequenceMatcher(None,candidate,brand).ratio()>=0.74):
                question=re.sub(r"\b"+re.escape(token)+r"\b",brand,question,flags=re.I)
        # Rewrite common generic requests to a canonical, business-specific
        # overview question. This is not an exact-string lookup: vector retrieval
        # still determines which approved fact (if any) may answer.
        question=re.sub(r"\b(?:can you (?:please )?tell me )?what (?:does|can) "+
            re.escape(brand)+r" do\b", "What is "+brand+"?",question,flags=re.I)
        question=re.sub(r"\b(?:can you (?:please )?tell me )?what "+
            re.escape(brand)+r" does\b", "What is "+brand+"?",question,flags=re.I)
    return question,brands

def retrieve(organization_id:str,question:str):
    policy,metadata=_approved_policy_fallback(organization_id,question,"no_policy_inference")
    if policy is not None:return policy,metadata
    normalized,brand_terms=_normalize_spoken_name(organization_id,question)
    vector=embeddings([normalized])[0]
    secret=os.getenv("SUPABASE_SECRET_KEY","")
    if not secret:raise HTTPException(503,"Database secret not configured")
    base=os.getenv("SUPABASE_URL","").rstrip("/")
    try:
        with httpx.Client(timeout=12) as c:
            response=c.post(base+"/rest/v1/rpc/match_callpilot_knowledge",
                headers={"apikey":secret,"Content-Type":"application/json"},
                json={"p_org_id":organization_id,"p_embedding":to_pgvector(vector),"p_limit":10})
    except httpx.RequestError:raise HTTPException(503,"Semantic database search unavailable")
    if response.status_code!=200:
        raise HTTPException(503,"Semantic database search failed (HTTP "+str(response.status_code)+")")
    rows=response.json()
    if not rows:return None,{"reason":"no_indexed_facts"}
    intent=_intent(normalized)
    query_terms=_terms(normalized)
    content_terms=query_terms-brand_terms
    ranked=[]
    for row in rows:
        similarity=float(row["similarity"])
        question_terms=_terms(row["question"])
        answer_terms=_terms(row["answer"])
        question_overlap=len(content_terms & question_terms)
        answer_overlap=len(content_terms & answer_terms)
        candidate_intent=_question_intent(row["question"])
        intent_match=bool(intent and candidate_intent==intent)
        intent_conflict=bool(intent and candidate_intent and candidate_intent!=intent)
        score=(similarity + 0.055*int(intent_match) - 0.03*int(intent_conflict)
               + 0.025*question_overlap + 0.006*min(answer_overlap,3))
        ranked.append((score,row,similarity,question_overlap,answer_overlap,intent_match))
    ranked.sort(key=lambda entry:entry[0],reverse=True)
    score,best,similarity,question_overlap,answer_overlap,intent_match=ranked[0]
    # Require strong semantic evidence AND the requested topic or recognized
    # conversational intent. Entity names alone are not evidence that an
    # unrelated service/product is covered by approved knowledge.
    is_description=best["question"].startswith("Business description — ")
    # Descriptions have much richer language than FAQ prompts. Permit a slightly
    # lower similarity only when the approved passage actually mentions the topic.
    evidence=(content_terms & (_terms(best["question"]) | _terms(best["answer"])))
    min_similarity=0.78 if is_description and evidence else 0.82
    if similarity<min_similarity:
        return None,{"reason":"below_confidence_threshold","similarity":round(similarity,3)}
    if content_terms and not question_overlap and not answer_overlap:
        return None,{"reason":"topic_not_supported","similarity":round(similarity,3)}
    # Generic nouns alone cannot justify answering a specific unknown product query.
    generic={"product","service","business","company","item","good","thing"}
    specific=content_terms-generic
    if is_description and specific and (specific & evidence)==set() and similarity<0.9:
        return None,{"reason":"unsupported_specific_topic","similarity":round(similarity,3)}
    if not content_terms and not intent_match and not (
        query_terms and query_terms & _terms(best["question"])):
        return None,{"reason":"insufficient_topic_evidence","similarity":round(similarity,3)}
    # "Do you sell X?" must find X itself in the approved passage. A category overview that merely
    # lists other things is not evidence that an unlisted item (say, live elephants) is offered.
    if re.search(r"\b(do you|do y'all|are you|can i|can you|have you got)\b.*\b(sell|carry|stock|have|offer|make|print|provide)\b",normalized.casefold()):
        wanted=specific-{"live","new","good","custom","cheap","best"}
        if wanted and not (wanted & (_terms(best["question"])|_terms(best["answer"]))):
            return None,{"reason":"item_not_listed","similarity":round(similarity,3)}
    # Owner-written passages are grounded answers, not FAQ prompts.
    # They have more detailed text than a short query; allow retrieval when
    # a strong vector match and a supported topic align. Still reject unrelated
    # questions rather than making up company policies.
    if len(ranked)>1 and score-ranked[1][0]<0.015 and not (
        question_overlap>ranked[1][3] or intent_match and not ranked[1][5]
        or is_description and (question_overlap>0 or answer_overlap>0)
            and similarity>=0.86):
        return None,{"reason":"ambiguous_retrieval","similarity":round(similarity,3)}
    return best,{"reason":"approved_description_passage" if is_description else
                 "approved_hybrid_semantic_match","similarity":round(similarity,3)}
