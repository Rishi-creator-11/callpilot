import os
from fastapi.testclient import TestClient
from app.main import app

client=TestClient(app)
def test_health():
    r=client.get("/health")
    assert r.status_code==200 and r.json()["status"]=="ok"
def test_admin_auth_denied_by_default(monkeypatch):
    monkeypatch.delenv("ADMIN_TOKEN",raising=False)
    assert client.get("/api/businesses").status_code==401
    assert client.get("/api/cloud/businesses").status_code==401
def test_business_knowledge_and_price_safety(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN","test-secret-not-a-real-key")
    h={"X-Admin-Token":"test-secret-not-a-real-key"}
    b=client.post("/api/businesses",headers=h,json={"name":"Test Auto Repair","industry":"Auto"}).json()
    oid=b["id"]
    q={"question":"What are your opening hours?"}
    r=client.post(f"/api/businesses/{oid}/knowledge",headers=h,json={**q,"answer":"We open weekdays at 9 AM.","approved":False})
    assert r.status_code==200
    a=client.post(f"/api/businesses/{oid}/simulate",headers=h,json=q).json()
    assert a["source"]=="unknown"
    price=client.post(f"/api/businesses/{oid}/simulate",headers=h,json={"question":"How much is the service?"}).json()
    assert price["source"]=="policy" and "price" in price["answer"].lower()
    rejected=client.post(f"/api/businesses/{oid}/knowledge",headers=h,json={"question":"What is the estimate?","answer":"About $100.","approved":True})
    assert rejected.status_code==400
def test_booking_request_not_confirmed(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN","test-secret-not-a-real-key")
    h={"X-Admin-Token":"test-secret-not-a-real-key"}
    b=client.post("/api/businesses",headers=h,json={"name":"Test Bookings","industry":"Auto"}).json()
    r=client.post(f"/api/businesses/{b['id']}/booking-requests",headers=h,json={"name":"John","phone":"555-0000","service":"Inspection","requested_time":"Friday next week"})
    assert r.status_code==200 and r.json()["confirmed"] is False
def test_custom_voice_cannot_be_configured_without_verification(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN","test-secret-not-a-real-key")
    h={"X-Admin-Token":"test-secret-not-a-real-key"}
    b=client.post("/api/businesses",headers=h,json={"name":"Test Voice","industry":"Auto"}).json()
    r=client.put(f"/api/businesses/{b['id']}/agent",headers=h,json={"name":"Alex","greeting":"Hello, I am the AI assistant.","voice_provider":"elevenlabs","voice_id":"arbitrary-id","owner_verified":False})
    assert r.status_code==400
def test_webhook_denied_without_configured_token(monkeypatch):
    monkeypatch.delenv("VAPI_WEBHOOK_TOKEN",raising=False)
    x=client.post("/webhooks/vapi/11111111-1111-1111-1111-111111111111",json={"message":{"type":"end-of-call-report"}})
    assert x.status_code==401
def test_legal_page_states_launch_policies():
    r=client.get("/legal")
    assert r.status_code==200
    for text in ("$49","$99","$179","100","300","600","$100","four minutes","automatic overage","recorded"):
        assert text.lower() in r.text.lower()
    assert "to be completed" in r.text  # owner-specific facts are flagged, never invented
    for text in ("Limitation of liability","Acceptable use","Indemnity","not for emergency services","non-refundable"):
        assert text.lower() in r.text.lower()
def test_customer_app_links_to_policies():
    assert "/legal" in client.get("/app").text
