"""Disabled-by-default Stripe Billing integration with signed webhook processing."""
import os,time,hmac,hashlib,json,uuid
from datetime import datetime,timezone
from urllib.parse import urlparse
import httpx
from fastapi import APIRouter,Depends,HTTPException,Request
from app.account import current_user,membership
from app.cloud import api
from app.common import public_base

router=APIRouter(tags=["billing"])
PLANS={"starter":{"name":"Starter","monthly_cents":4900,"included_minutes":100},
       "growth":{"name":"Growth","monthly_cents":9900,"included_minutes":300},
       "business":{"name":"Business","monthly_cents":17900,"included_minutes":600}}
SETUP_FEE_CENTS=10000

def billing_mode():
    mode=os.getenv("STRIPE_MODE","test").lower()
    return mode if mode in ("test","live") else "invalid"

def billing_ready():
    if os.getenv("STRIPE_BILLING_ENABLED","").lower()!="true":
        return False
    mode=billing_mode()
    if mode not in ("test","live"):
        return False
    key=os.getenv("STRIPE_SECRET_KEY","")
    if not key.startswith(("sk_"+mode+"_","rk_"+mode+"_")):
        return False
    if not os.getenv("STRIPE_WEBHOOK_SECRET","").startswith("whsec_"):
        return False
    if mode=="live":
        return (os.getenv("STRIPE_LIVE_APPROVED","").lower()=="true"
            and all(os.getenv("STRIPE_PRICE_"+plan.upper(),"").startswith("price_") for plan in PLANS)
            and os.getenv("STRIPE_PRICE_SETUP","").startswith("price_"))
    return True

def stripe_call(path,method="POST",data=None):
    mode=billing_mode()
    key=os.getenv("STRIPE_SECRET_KEY","")
    if not billing_ready() or not key.startswith(("sk_"+mode+"_","rk_"+mode+"_")):
        raise HTTPException(503,"Stripe checkout is not configured")
    try:
        with httpx.Client(timeout=15) as client:
            r=client.request(method,"https://api.stripe.com/v1/"+path,
                auth=(key,""),data=data)
    except httpx.RequestError:
        raise HTTPException(503,"Stripe service unavailable")
    if r.status_code>=400:
        raise HTTPException(502,"Stripe request could not be completed")
    return r.json()

def verify_live_prices():
    if billing_mode()!="live":
        return
    expected={key:plan["monthly_cents"] for key,plan in PLANS.items()}
    expected["setup"]=SETUP_FEE_CENTS
    for name,cents in expected.items():
        pid=os.environ["STRIPE_PRICE_"+name.upper()]
        price=stripe_call("prices/"+pid,method="GET")
        monthly=name!="setup"
        if (price.get("currency")!="usd" or price.get("unit_amount")!=cents or
            price.get("active") is not True or
            (price.get("type")=="recurring")!=monthly or
            (monthly and (price.get("recurring") or {}).get("interval")!="month")):
            raise HTTPException(409,"Stripe product prices do not match CallPilot plans")

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
        "pending_confirmation":bool(a and a.get("pending_checkout_session_id") and not (a.get("status") in ("active","trialing"))),
        "cancel_at_period_end":bool(a and a.get("cancel_at_period_end")),
        "mode":("sandbox" if billing_mode()=="test" else "live") if billing_ready() else "not_enabled",
        "live_ready":billing_ready() and billing_mode()=="live"}

from pydantic import BaseModel
from typing import Literal

class CheckoutSelection(BaseModel):
    plan:Literal["starter","growth","business"]

@router.post("/api/account/organizations/{oid}/checkout")
def checkout(oid:str,selection:CheckoutSelection,user=Depends(current_user)):
    membership(oid,user,("owner",))
    if not billing_ready():
        raise HTTPException(503,"Checkout is disabled until billing integration is verified")
    if billing_mode()=="live":
        verify_live_prices()
    a=account(oid)
    if a and a.get("stripe_customer_id") and a.get("stripe_mode","test")!=billing_mode():
        raise HTTPException(409,"This workspace has a sandbox customer. Contact support before switching to live billing.")
    if a and a.get("status") in ("active","trialing"):
        raise HTTPException(409,"An active subscription already exists. Use the billing portal.")
    customer=(a or {}).get("stripe_customer_id")
    if not customer:
        c=stripe_call("customers",data={"email":user["email"],"metadata[organization_id]":oid})
        customer=c["id"]
        api("billing_accounts","POST",params={"on_conflict":"organization_id"},
            data={"organization_id":oid,"stripe_customer_id":customer,
                  "stripe_mode":billing_mode()},
            prefer="resolution=merge-duplicates,return=minimal")
    plan=PLANS[selection.plan]
    base=public_base()
    data={
        "mode":"subscription","customer":customer,
        "line_items[0][quantity]":"1",
        "client_reference_id":oid,
        "metadata[organization_id]":oid,
        "metadata[plan]":selection.plan,
        "subscription_data[metadata][organization_id]":oid,
        "subscription_data[metadata][plan]":selection.plan,
        "success_url":base+"/app?billing=success#billing",
        "cancel_url":base+"/app?billing=cancelled#billing"}
    if billing_mode()=="live":
        data["line_items[0][price]"]=os.environ["STRIPE_PRICE_"+selection.plan.upper()]
    else:
        data.update({"line_items[0][price_data][currency]":"usd",
          "line_items[0][price_data][unit_amount]":str(plan["monthly_cents"]),
          "line_items[0][price_data][recurring][interval]":"month",
          "line_items[0][price_data][product_data][name]":"CallPilot "+plan["name"]+" subscription"})
    if not (a or {}).get("setup_paid_at"):
        if billing_mode()=="live":
            data.update({"line_items[1][price]":os.environ["STRIPE_PRICE_SETUP"],
                         "line_items[1][quantity]":"1",
                         "metadata[setup_applied]":"true"})
        else:
            data.update({
            "line_items[1][price_data][currency]":"usd",
            "line_items[1][price_data][unit_amount]":str(SETUP_FEE_CENTS),
            "line_items[1][price_data][product_data][name]":"One-time professional AI receptionist setup",
            "line_items[1][quantity]":"1",
            "metadata[setup_applied]":"true"})
    # Only one checkout may be open per workspace, so the one-time setup fee can never be paid twice.
    previous=(a or {}).get("pending_checkout_session_id")
    if previous:
        try:stripe_call("checkout/sessions/"+previous+"/expire")
        except HTTPException:pass
    session=stripe_call("checkout/sessions",data=data)
    api("billing_accounts","PATCH",params={"organization_id":"eq."+oid},
        data={"pending_checkout_session_id":session.get("id")},prefer="return=minimal")
    return {"url":session["url"],"mode":"sandbox" if billing_mode()=="test" else "live","plan":selection.plan}

@router.post("/api/account/organizations/{oid}/billing/confirm-checkout")
def confirm_checkout(oid:str,user=Depends(current_user)):
    """Reconcile the server-recorded pending checkout after redirect.

    Never trusts a browser-provided session or a successful URL. Stripe must
    verify payment, customer, tenant and the existing checkout session.
    Signed webhooks remain the authoritative asynchronous update path.
    """
    membership(oid,user,("owner",))
    a=account(oid)
    if not a or not a.get("pending_checkout_session_id"):
        return {"confirmed":bool(a and a.get("status") in ("active","trialing")),
                "status":(a or {}).get("status","unconfigured")}
    if not billing_ready() or a.get("stripe_mode","test")!=billing_mode():
        raise HTTPException(503,"Stripe reconciliation unavailable")
    checkout_id=a["pending_checkout_session_id"]
    session=stripe_call("checkout/sessions/"+checkout_id,method="GET")
    if (session.get("id")!=checkout_id or
        session.get("client_reference_id")!=oid or
        (session.get("metadata") or {}).get("organization_id")!=oid or
        session.get("customer")!=a.get("stripe_customer_id") or
        session.get("mode")!="subscription" or
        session.get("livemode")!=(billing_mode()=="live")):
        raise HTTPException(409,"Stripe checkout does not match this workspace")
    if session.get("status")!="complete" or session.get("payment_status")!="paid":
        return {"confirmed":False,"status":"payment_pending"}
    subscription_id=session.get("subscription")
    if not isinstance(subscription_id,str) or not subscription_id.startswith("sub_"):
        return {"confirmed":False,"status":"subscription_pending"}
    sub=stripe_call("subscriptions/"+subscription_id,method="GET")
    if (sub.get("id")!=subscription_id or
        sub.get("customer")!=a.get("stripe_customer_id") or
        sub.get("livemode")!=(billing_mode()=="live")):
        raise HTTPException(409,"Stripe subscription does not match this workspace")
    if sub.get("status") not in ("active","trialing"):
        return {"confirmed":False,"status":sub.get("status","pending")}
    handle_stripe_event("checkout.session.completed",session)
    handle_stripe_event("customer.subscription.updated",sub)
    return {"confirmed":True,"status":sub["status"],
            "selected_plan":(account(oid) or {}).get("selected_plan")}

@router.post("/api/account/organizations/{oid}/billing-portal")
def billing_portal(oid:str,user=Depends(current_user)):
    membership(oid,user,("owner",))
    a=account(oid)
    if not a or not a.get("stripe_customer_id"):raise HTTPException(404,"No billing account")
    if not billing_ready() or a.get("stripe_mode","test")!=billing_mode():
        raise HTTPException(503,"The configured billing environment does not match this account")
    session=stripe_call("billing_portal/sessions",data={
        "customer":a["stripe_customer_id"],
        "return_url":public_base()+"/app"})
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
    e_type=event.get("type","")
    if event.get("livemode") != (billing_mode()=="live"):
        raise HTTPException(400,"Wrong Stripe payment environment")
    obj=event.get("data",{}).get("object",{})
    # Atomically claim the event id so concurrent or repeated deliveries are processed once.
    claimed=api("stripe_webhook_events","POST",params={"on_conflict":"id"},
        data={"id":eid,"event_type":e_type},
        prefer="resolution=ignore-duplicates,return=representation")
    if not claimed:return {"received":True,"duplicate":True}
    try:
        handle_stripe_event(e_type,obj)
    except Exception:
        # Release the claim so Stripe's retry can process the event again.
        try:api("stripe_webhook_events","DELETE",params={"id":"eq."+eid},prefer="return=minimal")
        except Exception:pass
        raise
    return {"received":True}

def handle_stripe_event(e_type,obj):
    if e_type in ("checkout.session.completed","checkout.session.async_payment_succeeded") and obj.get("mode")=="subscription":
        oid=(obj.get("metadata") or {}).get("organization_id")
        customer=obj.get("customer")
        if oid and customer and obj.get("payment_status")=="paid":
            rows=api("billing_accounts",params={
                "organization_id":"eq."+str(oid),
                "stripe_customer_id":"eq."+str(customer),
                "select":"organization_id,setup_paid_at,setup_session_id,pending_checkout_session_id"})
            if rows:
                patch={"last_checkout_session_id":obj.get("id"),
                       "selected_plan":(obj.get("metadata") or {}).get("plan")}
                if rows[0].get("pending_checkout_session_id")==obj.get("id"):
                    patch["pending_checkout_session_id"]=None
                if (obj.get("metadata") or {}).get("setup_applied")=="true":
                    if not rows[0].get("setup_paid_at"):
                        patch["setup_paid_at"]=datetime.now(timezone.utc).isoformat()
                        patch["setup_session_id"]=obj.get("id")
                    elif rows[0].get("setup_session_id")!=obj.get("id"):
                        # A second setup fee was somehow paid. Never auto-refund; flag for the operator.
                        patch["billing_review_note"]=("Duplicate $100 setup fee paid in checkout session "
                            +str(obj.get("id"))+"; manual refund review required")
                api("billing_accounts","PATCH",
                    params={"organization_id":"eq."+str(oid),"stripe_customer_id":"eq."+str(customer)},
                    data=patch,prefer="return=minimal")
    if e_type=="checkout.session.async_payment_failed" and obj.get("mode")=="subscription":
        customer=obj.get("customer")
        if customer:
            rows=api("billing_accounts",params={"stripe_customer_id":"eq."+str(customer),
                  "select":"organization_id","limit":1})
            if rows:
                api("billing_accounts","PATCH",
                    params={"organization_id":"eq."+rows[0]["organization_id"]},
                    data={"status":"incomplete","updated_at":datetime.now(timezone.utc).isoformat()},
                    prefer="return=minimal")
    if e_type=="invoice.payment_failed":
        customer=obj.get("customer")
        subscription=obj.get("subscription")
        if customer:
            matching=api("billing_accounts",params={"stripe_customer_id":"eq."+str(customer),
                "select":"organization_id,stripe_subscription_id"})
            for row in matching:
                if subscription and row.get("stripe_subscription_id")!=subscription:
                    continue
                api("billing_accounts","PATCH",
                    params={"organization_id":"eq."+row["organization_id"]},
                    data={"status":"past_due","updated_at":datetime.now(timezone.utc).isoformat()},
                    prefer="return=minimal")
    if e_type in ("customer.subscription.created","customer.subscription.updated","customer.subscription.deleted"):
        customer=obj.get("customer")
        subscriptions=api("billing_accounts",params={"stripe_customer_id":"eq."+str(customer),
            "stripe_mode":"eq."+billing_mode(),"select":"organization_id"})
        if subscriptions:
            oid=subscriptions[0]["organization_id"]
            item=(obj.get("items",{}).get("data") or [{}])[0]
            p=item.get("price",{})
            info={"stripe_subscription_id":obj.get("id"),
                "status":obj.get("status","unknown"),
                "price_id":p.get("id"),
                "cancel_at_period_end":bool(obj.get("cancel_at_period_end")),
                "updated_at":datetime.now(timezone.utc).isoformat()}
            plan=(obj.get("metadata") or {}).get("plan")
            # Stripe's customer portal can change the price without changing metadata.
            # The actual fixed live Price ID is authoritative when present.
            if billing_mode()=="live":
                for name in PLANS:
                    if p.get("id")==os.getenv("STRIPE_PRICE_"+name.upper()):
                        plan=name
                        break
            if plan in PLANS:info["selected_plan"]=plan
            # Stripe API versions may store the billing cycle on the item.
            for field in ("current_period_start","current_period_end"):
                epoch=obj.get(field) or item.get(field)
                if isinstance(epoch,(int,float)) and epoch>0:
                    info[field]=datetime.fromtimestamp(epoch,timezone.utc).isoformat()
            api("billing_accounts","PATCH",params={"organization_id":"eq."+oid},data=info,prefer="return=minimal")
