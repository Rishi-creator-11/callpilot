"""Off-network tests for catalog tool safety and order-request semantics."""
import pytest
from app import voice_actions

OID="b550c0af-7d9c-4231-8eec-cb9f6ba9e724"
CALL={"call":{"id":"call-test-8888","assistantId":"voice-owned-here","type":"inboundPhoneCall"}}
PRODUCT={"id":"00000000-0000-4000-8000-000000000002","sku":"CARDS-1000","name":"Business cards 1000-pack",
         "description":"Premium cardstock", "unit_price_cents":1999,"stock_quantity":100}

def capabilities(**changes):
    return {"provider_agent_id":"voice-owned-here","voice_catalog_enabled":True,
            "voice_order_requests_enabled":True,"allow_price_quotes":False,**changes}

def test_catalog_requires_exact_owner_approved_product(monkeypatch):
    monkeypatch.setattr(voice_actions,"feature_flags",lambda oid:capabilities())
    def fake_api(table,method="GET",params=None,data=None,prefer=None):
        assert params.get("organization_id")=="eq."+OID
        return [PRODUCT] if table=="catalog_items" else []
    monkeypatch.setattr(voice_actions,"api",fake_api)
    reply=voice_actions.lookup_catalog_item(OID,CALL,{"item":"CARDS-1000"})
    assert "Business cards" in reply and "100 units" in reply
    assert "$19.99" not in reply
    reply=voice_actions.lookup_catalog_item(OID,CALL,{"item":"nonsense item"})
    assert "cannot find" in reply

def test_catalog_prices_require_owner_permission(monkeypatch):
    monkeypatch.setattr(voice_actions,"feature_flags",lambda oid:capabilities(allow_price_quotes=True))
    monkeypatch.setattr(voice_actions,"api",lambda *a,**kw:[PRODUCT])
    assert "$19.99" in voice_actions.lookup_catalog_item(OID,CALL,{"item":"CARDS-1000"})

def test_order_request_does_not_accept_implicit_yes(monkeypatch):
    monkeypatch.setattr(voice_actions,"feature_flags",lambda oid:capabilities())
    with pytest.raises(ValueError,match="agreement"):
        voice_actions.submit_order_request(OID,CALL,{"sku":"CARDS-1000","quantity":1,
           "caller_name":"Alex Johnson","caller_contact":"+15555550901","confirmed":False})

def test_order_request_respects_recorded_stock(monkeypatch):
    monkeypatch.setattr(voice_actions,"feature_flags",lambda oid:capabilities())
    monkeypatch.setattr(voice_actions,"api",lambda *a,**kw:[PRODUCT])
    reply=voice_actions.submit_order_request(OID,CALL,{"sku":"CARDS-1000","quantity":200,
       "caller_name":"Alex Johnson","caller_contact":"+15555550901","confirmed":True})
    assert "No order request was submitted" in reply

def test_order_request_is_pending_and_idempotent(monkeypatch):
    monkeypatch.setattr(voice_actions,"feature_flags",lambda oid:capabilities())
    writes=[];existing=[]
    def api(table,method="GET",params=None,data=None,prefer=None):
        if method=="GET":assert params.get("organization_id")=="eq."+OID
        if table=="catalog_items":return [PRODUCT]
        if table=="order_requests":
            if method=="GET":return existing
            writes.append(data)
            existing.append({"id":"00000000-0000-4000-8000-000000000003","status":"requested"})
            return [{"id":existing[0]["id"]}]
        return []
    monkeypatch.setattr(voice_actions,"api",api)
    args={"sku":"CARDS-1000","quantity":2,"caller_name":"Alex Johnson",
          "caller_contact":"+15555550901","confirmed":True}
    first=voice_actions.submit_order_request(OID,CALL,args)
    second=voice_actions.submit_order_request(OID,CALL,args)
    assert "No payment" in first
    assert "already saved" in second
    assert len(writes)==1 and writes[0]["status"]=="requested"
    assert writes[0]["quantity"]==2 and writes[0]["sku"]=="CARDS-1000"

def test_catalog_cross_tenant_assistant_rejected(monkeypatch):
    monkeypatch.setattr(voice_actions,"feature_flags",
                        lambda oid:capabilities(provider_agent_id="someone-elses-agent"))
    with pytest.raises(ValueError,match="does not belong"):
        voice_actions.lookup_catalog_item(OID,CALL,{"item":"CARDS-1000"})
