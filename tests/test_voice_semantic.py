from fastapi.testclient import TestClient
import app.vapi as vapi
import app.semantic as semantic
from app.main import app

client=TestClient(app)

def test_voice_semantic_tool_needs_credential(monkeypatch):
    monkeypatch.delenv("VAPI_WEBHOOK_TOKEN",raising=False)
    r=client.post("/webhooks/vapi-tools/test",json={"message":{"type":"tool-calls","toolCallList":[]}})
    assert r.status_code==401

def test_voice_semantic_tool_returns_only_approved_answer(monkeypatch):
    monkeypatch.setenv("VAPI_WEBHOOK_TOKEN","only-for-tests")
    monkeypatch.setattr(vapi,"org_or_404",lambda oid:{"id":oid})
    def lookup(org, question):
        assert org=="business-1"
        assert question=="Can I buy half container?"
        return {"question":"Can I purchase half container?","answer":"No, we only sell full containers."},{"similarity":0.99}
    monkeypatch.setattr(semantic,"retrieve",lookup)
    r=client.post("/webhooks/vapi-tools/business-1",headers={"Authorization":"Bearer only-for-tests"},
       json={"message":{"type":"tool-calls","toolCallList":[{"id":"call-1","function":{"name":"lookup_approved_knowledge","arguments":{"question":"Can I buy half container?"}}}]}})
    assert r.status_code==200
    assert r.json()=={"results":[{"toolCallId":"call-1","result":"No, we only sell full containers."}]}

def test_voice_pricing_policy_overrides_retrieval(monkeypatch):
    monkeypatch.setenv("VAPI_WEBHOOK_TOKEN","only-for-tests")
    monkeypatch.setattr(vapi,"org_or_404",lambda oid:{"id":oid})
    monkeypatch.setattr("app.account.tenant_prices_enabled",lambda oid: False)
    monkeypatch.setattr(semantic,"retrieve",lambda *args: (_ for _ in ()).throw(AssertionError("Should not call retrieval for pricing")))
    r=client.post("/webhooks/vapi-tools/business-1",headers={"Authorization":"Bearer only-for-tests"},
       json={"message":{"type":"tool-calls","toolCallList":[{"id":"call-2","function":{"name":"lookup_approved_knowledge","arguments":{"question":"How much is the container?"}}}]}})
    assert r.status_code==200
    assert "exact quote" in r.json()["results"][0]["result"].lower()


def _fake_rows(monkeypatch, rows):
    class Response:
        status_code = 200
        def json(self): return rows
    class Http:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, *a, **k): return Response()
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "x")
    monkeypatch.setenv("SUPABASE_URL", "https://example.test")
    monkeypatch.setattr(semantic.httpx, "Client", Http)
    monkeypatch.setattr(semantic, "embeddings", lambda texts: [[0.0] * 3 for _ in texts])
    monkeypatch.setattr(semantic, "to_pgvector", lambda v: "[0,0,0]")
    monkeypatch.setattr(semantic, "_approved_policy_fallback", lambda *a: (None, {}))
    monkeypatch.setattr(semantic, "_normalize_spoken_name", lambda oid, q: (q, set()))


OVERVIEW = {"question": "What do you sell?", "similarity": 0.9,
            "answer": "We offer products and services in these categories: Business Cards, Flyers and Banners."}


def test_unlisted_item_is_not_answered_from_a_category_overview(monkeypatch):
    _fake_rows(monkeypatch, [OVERVIEW])
    best, meta = semantic.retrieve("org", "Do you sell live elephants?")
    assert best is None and meta["reason"] in ("item_not_listed", "topic_not_supported")


def test_listed_item_is_still_answered_from_a_category_overview(monkeypatch):
    _fake_rows(monkeypatch, [OVERVIEW])
    best, _ = semantic.retrieve("org", "Do you sell flyers?")
    assert best is not None and "Flyers" in best["answer"]
