"""Billing: setup fee only once, mode safety, idempotent signed webhooks, tenant-safe updates."""
import hmac, hashlib, json, time
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app import billing
from app.account import current_user

client=TestClient(app)
OID="00000000-0000-0000-0000-0000000000a1"
OTHER="00000000-0000-0000-0000-0000000000b2"
SECRET="whsec_test_secret_for_unit_tests"

class FakeDB:
    """Tiny in-memory stand-in for the Supabase REST helper (eq. filters only)."""
    def __init__(self):
        self.t={"billing_accounts":[],"stripe_webhook_events":[]}
    def _match(self,row,params):
        for k,v in (params or {}).items():
            if k in ("select","limit","on_conflict","order"):continue
            if isinstance(v,str) and v.startswith("eq."):
                if str(row.get(k))!=v[3:]:return False
        return True
    def __call__(self,table,method="GET",params=None,data=None,prefer=None):
        rows=self.t.setdefault(table,[])
        if method=="GET":return [dict(r) for r in rows if self._match(r,params)]
        if method=="POST":
            key=(params or {}).get("on_conflict")
            existing=[r for r in rows if key and r.get(key)==data.get(key)]
            if existing:
                if "ignore-duplicates" in (prefer or ""):return []
                existing[0].update(data);return []
            rows.append(dict(data));return [dict(data)] if "representation" in (prefer or "") else []
        if method=="PATCH":
            for r in rows:
                if self._match(r,params):r.update(data)
            return []
        if method=="DELETE":
            self.t[table]=[r for r in rows if not self._match(r,params)];return []

@pytest.fixture
def db(monkeypatch):
    d=FakeDB()
    monkeypatch.setattr(billing,"api",d)
    monkeypatch.setattr(billing,"membership",lambda *a,**k:"owner")
    monkeypatch.setenv("STRIPE_MODE","test")
    monkeypatch.setenv("STRIPE_SECRET_KEY","sk_test_unit")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET",SECRET)
    monkeypatch.setenv("STRIPE_BILLING_ENABLED","true")
    app.dependency_overrides[current_user]=lambda:{"id":"u1","email":"owner@example.com"}
    yield d
    app.dependency_overrides.clear()

def post_checkout(monkeypatch,plan="starter"):
    calls=[]
    def fake_stripe(path,method="POST",data=None):
        calls.append((path,data))
        if path=="customers":return {"id":"cus_1"}
        if path.endswith("/expire"):return {}
        return {"id":"cs_"+str(len(calls)),"url":"https://checkout.stripe.test/s"}
    monkeypatch.setattr(billing,"stripe_call",fake_stripe)
    r=client.post(f"/api/account/organizations/{OID}/checkout",json={"plan":plan})
    return r,calls

def session_data(calls):
    return [d for p,d in calls if p=="checkout/sessions"][0]

def test_first_checkout_includes_the_100_dollar_setup_fee(db,monkeypatch):
    r,calls=post_checkout(monkeypatch)
    assert r.status_code==200
    d=session_data(calls)
    assert d["line_items[0][price_data][unit_amount]"]=="4900"
    assert d["line_items[1][price_data][unit_amount]"]=="10000"
    assert d["metadata[setup_applied]"]=="true"

def test_plan_prices_match_the_published_offer(db,monkeypatch):
    for plan,cents in (("starter","4900"),("growth","9900"),("business","17900")):
        r,calls=post_checkout(monkeypatch,plan)
        assert session_data(calls)["line_items[0][price_data][unit_amount]"]==cents

def test_renewal_or_resubscribe_never_charges_setup_again(db,monkeypatch):
    db.t["billing_accounts"].append({"organization_id":OID,"stripe_customer_id":"cus_1",
        "stripe_mode":"test","status":"canceled","setup_paid_at":"2026-11-01T00:00:00+00:00"})
    r,calls=post_checkout(monkeypatch,"growth")
    assert r.status_code==200
    d=session_data(calls)
    assert "line_items[1][quantity]" not in d and "metadata[setup_applied]" not in d

def test_second_checkout_expires_the_first_open_session(db,monkeypatch):
    post_checkout(monkeypatch)
    first=db.t["billing_accounts"][0]["pending_checkout_session_id"]
    assert first
    r,calls=post_checkout(monkeypatch)
    assert any(p=="checkout/sessions/"+first+"/expire" for p,_ in calls)

def test_active_subscription_cannot_check_out_again(db,monkeypatch):
    db.t["billing_accounts"].append({"organization_id":OID,"stripe_customer_id":"cus_1",
        "stripe_mode":"test","status":"active"})
    r,_=post_checkout(monkeypatch)
    assert r.status_code==409

def test_sandbox_customer_is_never_used_in_live_mode(db,monkeypatch):
    monkeypatch.setenv("STRIPE_MODE","live"); monkeypatch.setenv("STRIPE_SECRET_KEY","sk_live_unit")
    monkeypatch.setenv("STRIPE_LIVE_APPROVED","true")
    for n in ("STARTER","GROWTH","BUSINESS","SETUP"):monkeypatch.setenv("STRIPE_PRICE_"+n,"price_x")
    monkeypatch.setattr(billing,"verify_live_prices",lambda:None)
    db.t["billing_accounts"].append({"organization_id":OID,"stripe_customer_id":"cus_sandbox","stripe_mode":"test"})
    r,_=post_checkout(monkeypatch)
    assert r.status_code==409
    assert client.post(f"/api/account/organizations/{OID}/billing-portal").status_code==503

def test_live_charging_stays_disabled_without_explicit_approval(monkeypatch):
    monkeypatch.setenv("STRIPE_BILLING_ENABLED","true"); monkeypatch.setenv("STRIPE_MODE","live")
    monkeypatch.setenv("STRIPE_SECRET_KEY","sk_live_unit"); monkeypatch.setenv("STRIPE_WEBHOOK_SECRET",SECRET)
    monkeypatch.delenv("STRIPE_LIVE_APPROVED",raising=False)
    assert billing.billing_ready() is False

def signed(event,secret=SECRET,age=0):
    body=json.dumps(event).encode()
    ts=int(time.time())-age
    sig=hmac.new(secret.encode(),str(ts).encode()+b"."+body,hashlib.sha256).hexdigest()
    return body,{"stripe-signature":f"t={ts},v1={sig}","content-type":"application/json"}

def hook(event,**kw):
    body,h=signed(event,**kw)
    return client.post("/webhooks/stripe",content=body,headers=h)

def completed(eid,session="cs_a",setup=True,oid=OID,customer="cus_1"):
    return {"id":eid,"type":"checkout.session.completed","livemode":False,"data":{"object":{
        "id":session,"mode":"subscription","customer":customer,"payment_status":"paid",
        "metadata":{"organization_id":oid,"plan":"growth","setup_applied":"true" if setup else "false"}}}}

def test_webhook_rejects_bad_signature_and_stale_timestamp(db):
    assert hook(completed("evt_1"),secret="whsec_wrong").status_code==400
    assert hook(completed("evt_2"),age=3600).status_code==400

def test_webhook_rejects_wrong_payment_environment(db):
    e=completed("evt_3"); e["livemode"]=True
    assert hook(e).status_code==400

def test_checkout_completed_is_processed_once(db):
    db.t["billing_accounts"].append({"organization_id":OID,"stripe_customer_id":"cus_1","stripe_mode":"test"})
    assert hook(completed("evt_10")).json()=={"received":True}
    row=db.t["billing_accounts"][0]
    assert row["setup_paid_at"] and row["setup_session_id"]=="cs_a" and row["selected_plan"]=="growth"
    paid_at=row["setup_paid_at"]
    assert hook(completed("evt_10")).json().get("duplicate") is True
    assert db.t["billing_accounts"][0]["setup_paid_at"]==paid_at

def test_duplicate_setup_payment_is_flagged_not_silently_accepted(db):
    db.t["billing_accounts"].append({"organization_id":OID,"stripe_customer_id":"cus_1","stripe_mode":"test",
        "setup_paid_at":"2026-11-01T00:00:00+00:00","setup_session_id":"cs_first"})
    hook(completed("evt_11",session="cs_second"))
    row=db.t["billing_accounts"][0]
    assert row["setup_session_id"]=="cs_first"
    assert "refund review" in row["billing_review_note"]

def test_failed_processing_releases_the_claim_so_stripe_can_retry(db,monkeypatch):
    db.t["billing_accounts"].append({"organization_id":OID,"stripe_customer_id":"cus_1","stripe_mode":"test"})
    def boom(*a,**k):raise RuntimeError("db down")
    monkeypatch.setattr(billing,"handle_stripe_event",boom)
    with pytest.raises(RuntimeError):hook(completed("evt_12"))
    assert db.t["stripe_webhook_events"]==[]

def test_checkout_webhook_for_another_customer_cannot_touch_this_workspace(db):
    db.t["billing_accounts"].append({"organization_id":OID,"stripe_customer_id":"cus_1","stripe_mode":"test"})
    hook(completed("evt_13",oid=OID,customer="cus_attacker"))
    assert not db.t["billing_accounts"][0].get("setup_paid_at")

def sub_event(eid,etype,customer="cus_1",status="active",cancel=False,price="price_x"):
    return {"id":eid,"type":etype,"livemode":False,"data":{"object":{
        "id":"sub_1","customer":customer,"status":status,"cancel_at_period_end":cancel,
        "metadata":{"plan":"business"},"current_period_start":1893456000,"current_period_end":1896048000,
        "items":{"data":[{"price":{"id":price}}]}}}}

def test_subscription_lifecycle_upgrade_cancel_and_period_sync(db):
    db.t["billing_accounts"].append({"organization_id":OID,"stripe_customer_id":"cus_1","stripe_mode":"test"})
    hook(sub_event("evt_20","customer.subscription.updated"))
    row=db.t["billing_accounts"][0]
    assert row["status"]=="active" and row["selected_plan"]=="business"
    assert row["current_period_start"] and row["current_period_end"]
    hook(sub_event("evt_21","customer.subscription.updated",cancel=True))
    assert db.t["billing_accounts"][0]["cancel_at_period_end"] is True
    hook(sub_event("evt_22","customer.subscription.deleted",status="canceled"))
    assert db.t["billing_accounts"][0]["status"]=="canceled"

def test_subscription_events_ignore_customers_from_another_stripe_mode(db):
    db.t["billing_accounts"].append({"organization_id":OID,"stripe_customer_id":"cus_1","stripe_mode":"live"})
    hook(sub_event("evt_23","customer.subscription.updated"))
    assert db.t["billing_accounts"][0].get("status") is None

def test_payment_failure_marks_past_due_and_recovery_reactivates(db):
    db.t["billing_accounts"].append({"organization_id":OID,"stripe_customer_id":"cus_1",
        "stripe_mode":"test","stripe_subscription_id":"sub_1","status":"active"})
    hook({"id":"evt_30","type":"invoice.payment_failed","livemode":False,
          "data":{"object":{"customer":"cus_1","subscription":"sub_1"}}})
    assert db.t["billing_accounts"][0]["status"]=="past_due"
    hook(sub_event("evt_31","customer.subscription.updated",status="active"))
    assert db.t["billing_accounts"][0]["status"]=="active"

def test_fixed_prices_are_used_in_test_mode_when_configured(db,monkeypatch):
    monkeypatch.setenv("STRIPE_PRICE_GROWTH","price_growth_1"); monkeypatch.setenv("STRIPE_PRICE_SETUP","price_setup_1")
    r,calls=post_checkout(monkeypatch,"growth")
    d=session_data(calls)
    assert d["line_items[0][price]"]=="price_growth_1" and d["line_items[1][price]"]=="price_setup_1"
    assert not any("price_data" in k for k in d)

def test_portal_plan_switch_is_recognised_from_the_fixed_price_id(db,monkeypatch):
    monkeypatch.setenv("STRIPE_PRICE_BUSINESS","price_business_1")
    db.t["billing_accounts"].append({"organization_id":OID,"stripe_customer_id":"cus_1","stripe_mode":"test","selected_plan":"starter"})
    e=sub_event("evt_40","customer.subscription.updated",price="price_business_1"); e["data"]["object"]["metadata"]={}
    hook(e)
    assert db.t["billing_accounts"][0]["selected_plan"]=="business"

def test_billing_portal_uses_the_callpilot_configuration(db,monkeypatch):
    monkeypatch.setenv("STRIPE_PORTAL_CONFIG_ID","bpc_callpilot_1")
    db.t["billing_accounts"].append({"organization_id":OID,"stripe_customer_id":"cus_1","stripe_mode":"test"})
    seen={}
    monkeypatch.setattr(billing,"stripe_call",lambda path,method="POST",data=None:seen.update(path=path,data=data) or {"url":"https://portal.test"})
    assert client.post(f"/api/account/organizations/{OID}/billing-portal").status_code==200
    assert seen["path"]=="billing_portal/sessions" and seen["data"]["configuration"]=="bpc_callpilot_1"
