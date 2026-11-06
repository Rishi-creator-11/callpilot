import os
import hmac,hashlib,time
from fastapi.testclient import TestClient
from app.main import app
from app.billing import check_signature
client=TestClient(app)

def test_sign_in_required_for_customer_workspaces(monkeypatch):
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY","test-public")
    monkeypatch.setenv("SUPABASE_URL","https://example.supabase.co")
    assert client.get("/api/account/me").status_code==401
    assert client.post("/api/account/organizations",json={"name":"Unauthorized business"}).status_code==401

def test_public_config_does_not_expose_secrets(monkeypatch):
    monkeypatch.setenv("SUPABASE_SECRET_KEY","very-private")
    monkeypatch.setenv("VAPI_API_KEY","very-private")
    monkeypatch.setenv("STRIPE_SECRET_KEY","very-private")
    result=client.get("/api/account/config")
    assert result.status_code==200
    assert "very-private" not in result.text
    assert "billing_enabled" in result.json()

def test_stripe_signature_verification():
    secret="whsec_test_only"
    body=b'{"id":"evt_test","type":"customer.subscription.updated"}'
    timestamp=str(int(time.time()))
    signature=hmac.new(secret.encode(),timestamp.encode()+b"."+body,hashlib.sha256).hexdigest()
    header="t="+timestamp+",v1="+signature
    assert check_signature(body,header,secret)
    assert not check_signature(body+b"changed",header,secret)
    assert not check_signature(body,header,"wrong-secret")
    assert not check_signature(body,"t=1,v1="+signature,secret)

def test_billing_disabled_without_approved_prices(monkeypatch):
    from app.billing import billing_ready
    monkeypatch.setenv("STRIPE_BILLING_ENABLED","false")
    monkeypatch.delenv("STRIPE_PRICE_ID",raising=False)
    assert not billing_ready()
