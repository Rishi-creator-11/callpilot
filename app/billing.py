"""Disabled-by-default Stripe Billing integration with signed webhook processing."""
import os,time,hmac,hashlib,json,uuid
from datetime import datetime,timezone
from urllib.parse import urlparse
import httpx
from fastapi import APIRouter,Depends,HTTPException,Request
from app.account import current_user,membership
from app.cloud import api

router=APIRouter(tags=["billing"])
PLANS={"starter":{"name":"Starter","monthly_cents":4900,"included_minutes":100},
       "growth":{"name":"Growth","monthly_cents":9900,"included_minutes":300},
       "business":{"name":"Business","monthly_cents":17900,"included_minutes":600}}
SETUP_FEE_CENTS=10000

def billing_ready():
    return (os.getenv("STRIPE_BILLING_ENABLED","").lower()=="true"
            and os.getenv("STRIPE_MODE")=="test"
            and os.getenv("STRIPE_SECRET_KEY","").startswith("sk_test_"))

def stripe_call(path,method="POST",data=None):
    key=os.getenv("STRIPE_SECRET_KEY","")
    if not key.startswith("sk_test_"):raise HTTPException(503,"Stripe test mode is not configured")
    try:
        with httpx.Client(timeout=15) as client:
            r=client.request(method,"https://api.stripe.com/v1/"+path,
                auth=(key,""),data=data)
    except httpx.RequestError:raise HTTPException(503,"Stripe unavailable")
    if r.status_code>=400:raise HTTPException(502,"Stripe request failed")
    return r.json()

def account(oid):
    rows=api("billing_accounts",params={"organization_id":"eq."+oid,"select":"*"})
    return rows[0] if rows else None

@router.get("/api/account/organizations/{oid}/billing")
def billing_status(oid:str,user=Depends(current_user)):
    membership(oid,user)
    a=account(oid)
    return {"status":(a or {}).get("status","not_configured"),"checkout_enabled":billing_ready(),
        "has_customer":bool(a and a.get("stripe_customer_id")),"plans":PLANS,
        "setup_fee_cents":SETUP_FEE_CENTS,"setup_paid":bool(a and a.get("setup_paid_at")),
        "selected_plan":(a or {}).get("selected_plan"),
        "mode":"sandbox" if billing_ready() else "not_enabled"}

from pydantic import BaseModel
from typing import Literal

class CheckoutSelection(BaseModel):
    plan:Literal["starter","growth","business"]

@router.post("/api/account/organizations/{oid}/checkout")
def checkout(oid:str,selection:CheckoutSelection,user=Depends(current_user)):
    membership(oid,user,("owner",))
    if not billing_ready():
        raise HTTPException(503,"Sandbox checkout is disabled until billing integration is verified")
    a=account(oid)
    if a and a.get("status") in ("active","trialing"):
        raise HTTPException(409,"An active subscription already exists. Use the billing portal.")
    customer=(a or {}).get("stripe_customer_id")
    if not customer:
        c=stripe_call("customers",data={"email":user["email"],"metadata[organization_id]":oid})
        customer=c["id"]
        api("billing_accounts","POST",params={"on_conflict":"organization_id"},
            data={"organization_id":oid,"stripe_customer_id":customer},
            prefer="resolution=merge-duplicates,return=minimal")
    plan=PLANS[selection.plan]
    base=os.getenv("PUBLIC_BASE_URL","https://callpilot-internal.onrender.com").rstrip("/")
    data={
        "mode":"subscription","customer":customer,
        "line_items[0][price_data][currency]":"usd",
        "line_items[0][price_data][unit_amount]":str(plan["monthly_cents"]),
        "line_items[0][price_data][recurring][interval]":"month",
        "line_items[0][price_data][product_data][name]":"CallPilot "+plan["name"]+" subscription",
        "line_items[0][quantity]":"1",
        "client_reference_id":oid,
        "metadata[organization_id]":oid,
        "metadata[plan]":selection.plan,
        "subscription_data[metadata][organization_id]":oid,
        "subscription_data[metadata][plan]":selection.plan,
        "success_url":base+"/app?billing=success#billing",
        "cancel_url":base+"/app?billing=cancelled#billing"}
    if not (a or {}).get("setup_paid_at"):
        data.update({
            "line_items[1][price_data][currency]":"usd",
            "line_items[1][price_data][unit_amount]":str(SETUP_FEE_CENTS),
            "line_items[1][price_data][product_data][name]":"One-time professional AI receptionist setup",
            "line_items[1][quantity]":"1",
            "metadata[setup_applied]":"true"})
    session=stripe_call("checkout/sessions",data=data)
    return {"url":session["url"],"mode":"sandbox","plan":selection.plan}

@router.post("/api/account/organizations/{oid}/billing-portal")
def billing_portal(oid:str,user=Depends(current_user)):
    membership(oid,user,("owner",))
    a=account(oid)
    if not a or not a.get("stripe_customer_id"):raise HTTPException(404,"No billing account")
    session=stripe_call("billing_portal/sessions",data={
        "customer":a["stripe_customer_id"],
        "return_url":"https://callpilot-internal.onrender.com/app"})
    return {"url":session["url"]}

def check_signature(payload:bytes,header:str,secret:str)->bool:
    if not secret or len(payload)>1024*1024:return False
    fields={}
    for item in header.split(","):
        if "=" in item:
            k,v=item.strip().split("=",1)
            fields.setdefault(k,[]).append(v)
    try: timestamp=int(fields.get("t",[""])[0])
    except ValueError:return False
    if abs(time.time()-timestamp)>300:return False
    digest=hmac.new(secret.encode(),str(timestamp).encode()+b"."+payload,hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(digest,v) for v in fields.get("v1",[]))

@router.post("/webhooks/stripe")
async def stripe_webhook(request:Request):
    secret=os.getenv("STRIPE_WEBHOOK_SECRET","")
    body=await request.body()
    if not check_signature(body,request.headers.get("stripe-signature",""),secret):
        raise HTTPException(400,"Invalid webhook signature")
    event=json.loads(body)
    eid=event.get("id","")
    if not isinstance(eid,str) or not eid.startswith("evt_"):raise HTTPException(400,"Invalid event")
    exists=api("stripe_webhook_events",params={"id":"eq."+eid,"select":"id"})
    if exists:return {"received":True,"duplicate":True}
    e_type=event.get("type","")
    obj=event.get("data",{}).get("object",{})
    if e_type=="checkout.session.completed" and obj.get("mode")=="subscription":
        oid=(obj.get("metadata") or {}).get("organization_id")
        customer=obj.get("customer")
        if oid and customer and obj.get("payment_status")=="paid":
            rows=api("billing_accounts",params={
                "organization_id":"eq."+str(oid),
                "stripe_customer_id":"eq."+str(customer),
                "select":"organization_id,setup_paid_at"})
            if rows:
                patch={"last_checkout_session_id":obj.get("id"),
                       "selected_plan":(obj.get("metadata") or {}).get("plan")}
                if (obj.get("metadata") or {}).get("setup_applied")=="true" and not rows[0].get("setup_paid_at"):
                    patch["setup_paid_at"]=datetime.now(timezone.utc).isoformat()
                api("billing_accounts","PATCH",
                    params={"organization_id":"eq."+str(oid),"stripe_customer_id":"eq."+str(customer)},
                    data=patch,prefer="return=minimal")
    if e_type in ("customer.subscription.created","customer.subscription.updated","customer.subscription.deleted"):
        customer=obj.get("customer")
        subscriptions=api("billing_accounts",params={"stripe_customer_id":"eq."+str(customer),"select":"organization_id"})
        if subscriptions:
            oid=subscriptions[0]["organization_id"]
            item=(obj.get("items",{}).get("data") or [{}])[0]
            p=item.get("price",{})
            info={"stripe_subscription_id":obj.get("id"),"status":obj.get("status","unknown"),
                "price_id":p.get("id"),
                "selected_plan":(obj.get("metadata") or {}).get("plan"),
                "updated_at":datetime.now(timezone.utc).isoformat()}
            if obj.get("current_period_end"):info["current_period_end"]=datetime.fromtimestamp(obj["current_period_end"],timezone.utc).isoformat()
            api("billing_accounts","PATCH",params={"organization_id":"eq."+oid},data=info,prefer="return=minimal")
    api("stripe_webhook_events","POST",data={"id":eid,"event_type":e_type},prefer="return=minimal")
    return {"received":True}
