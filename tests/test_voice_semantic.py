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
    monkeypatch.setattr(semantic,"retrieve",lambda *args: (_ for _ in ()).throw(AssertionError("Should not call retrieval for pricing")))
    r=client.post("/webhooks/vapi-tools/business-1",headers={"Authorization":"Bearer only-for-tests"},
       json={"message":{"type":"tool-calls","toolCallList":[{"id":"call-2","function":{"name":"lookup_approved_knowledge","arguments":{"question":"How much is the container?"}}}]}})
    assert r.status_code==200
    assert "can't quote" in r.json()["results"][0]["result"].lower()
