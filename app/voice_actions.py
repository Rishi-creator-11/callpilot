"""Server-side, tenant-isolated voice actions for Vapi tools.

No tool is exposed unless the organization owner enables the capability.
All external writes require explicit confirmation in the prompt and arguments.
These server-side guards do not replace call recording/consent QA.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone, timedelta
from typing import Any
from uuid import UUID

import httpx
from fastapi import HTTPException
from app.cloud import api, public_base

MANAGED_TOOLS = frozenset({
    "capture_customer_lead", "check_appointment_availability", "book_appointment",
    "lookup_catalog_item", "submit_order_request", "resolve_call_menu",
})


def is_managed_tool_name(name) -> bool:
    return isinstance(name, str) and (name in MANAGED_TOOLS or re.fullmatch(r"api_[a-z0-9_]{2,40}", name) is not None)


def feature_flags(oid: str) -> dict:
    rows = api("agent_configs", params={
        "organization_id": "eq." + oid,
        "select": "provider_agent_id,voice_leads_enabled,voice_booking_enabled,allow_price_quotes,voice_catalog_enabled,voice_order_requests_enabled,call_instructions,worker_playbooks,speech_language,speech_pronunciations,transfer_directory,call_menu",
        "limit": 1,
    })
    return rows[0] if rows else {}


def _definition(name: str, description: str, properties: dict, required: list,
                oid: str, credential: str) -> dict:
    return {
        "type": "function", "async": False,
        "function": {
            "name": name, "description": description, "strict": True,
            "parameters": {"type": "object", "properties": properties,
                           "required": required, "additionalProperties": False},
        },
        "server": {
            "url": public_base() + "/webhooks/vapi-tools/" + oid,
            "credentialId": credential, "timeoutSeconds": 20,
        },
    }


def voice_tool_definitions(oid: str, credential: str, flags: dict) -> list:
    tools = []
    s = lambda explanation: {"type": "string", "description": explanation}
    if (flags.get("call_menu") or {}).get("enabled"):
        tools.append(_definition(
            "resolve_call_menu",
            "List the business's spoken menu options with an empty choice. To resolve a caller's choice, "
            "pass the exact words or option number they spoke. Never infer or invent their selection. "
            "This tool chooses the next workflow; it never performs a booking, transfer, or order.",
            {"spoken_choice": s("The caller's exact menu choice, or an empty string to list choices.")},
            ["spoken_choice"], oid, credential,
        ))
    if flags.get("voice_leads_enabled"):
        tools.append(_definition(
            "capture_customer_lead",
            "Persist a lead/callback request only after asking the caller for permission. "
            "Do not invent a name or number. A completed tool result confirms the lead "
            "was actually recorded; it does not mean a team member has already called.",
            {
                "caller_name": s("Name stated by the caller, or empty if none."),
                "callback_phone": s("Caller-confirmed callback number, or empty to use the incoming number."),
                "interest": s("Concise requested service or product, explicitly stated."),
                "callback_requested": {"type": "boolean",
                                       "description": "True only if the caller agrees to be contacted."},
            },
            ["caller_name", "callback_phone", "interest", "callback_requested"],
            oid, credential,
        ))
    if flags.get("voice_booking_enabled"):
        tools.append(_definition(
            "check_appointment_availability",
            "Read-only Google Calendar availability check. Never claim a slot was booked.",
            {
                "start": s("Full ISO-8601 datetime with the business timezone offset, for example 2026-11-09T10:00:00-05:00."),
                "duration_minutes": {"type": "integer", "minimum": 15, "maximum": 120},
            },
            ["start", "duration_minutes"], oid, credential,
        ))
        tools.append(_definition(
            "book_appointment",
            "Create a real Google Calendar appointment. Only invoke after "
            "checking availability, collecting name, contact, purpose, date/time "
            "and reading the details aloud, then hearing an explicit yes from "
            "the caller. Never treat selecting a slot as consent to book.",
            {
                "start": s("Full ISO-8601 datetime with timezone offset."),
                "duration_minutes": {"type": "integer", "minimum": 15, "maximum": 120},
                "caller_name": s("Caller-stated name."),
                "caller_contact": s("Caller-confirmed phone or email to reach them."),
                "service": s("Service requested, not a generic appointment."),
                "confirmed": {"type": "boolean",
                              "description": "True only after the caller explicitly agreed to the read-back."},
            },
            ["start", "duration_minutes", "caller_name", "caller_contact", "service", "confirmed"],
            oid, credential,
        ))
    if flags.get("voice_catalog_enabled"):
        tools.append(_definition(
            "lookup_catalog_item",
            "Read-only lookup of an owner-approved SKU or exact product name. "
            "Report live Shopify stock only if the result confirms it; otherwise qualify the source. "
            "Prices may be quoted only with the owner's separate pricing permission.",
            {"item": s("Product SKU or exact product name stated by the caller.")},
            ["item"], oid, credential,
        ))
    if flags.get("voice_order_requests_enabled"):
        tools.append(_definition(
            "submit_order_request",
            "Save a pending order request for business staff review only. No payment "
            "is taken, no stock is deducted, and no order is placed with a supplier. "
            "Use only after reading back the exact product, quantity, contact and "
            "getting explicit caller agreement to SUBMIT A REQUEST.",
            {
                "sku": s("Exact approved product SKU."),
                "quantity": {"type":"integer","minimum":1,"maximum":100000},
                "caller_name": s("Customer-stated name."),
                "caller_contact": s("Customer-confirmed phone number or email."),
                "confirmed": {"type":"boolean",
                              "description":"True only when caller agreed to submit a pending request."},
            },
            ["sku","quantity","caller_name","caller_contact","confirmed"],
            oid, credential,
        ))
    from app.custom_apis import tool_definitions
    tools.extend(tool_definitions(oid, credential, _definition))
    return tools


def _text(value: Any, *, maximum: int, minimum: int = 0) -> str:
    if not isinstance(value, str):
        raise ValueError("Expected text")
    clean = " ".join(value.split())
    if not minimum <= len(clean) <= maximum:
        raise ValueError("Invalid field length")
    return clean


def _call_context(oid: str, message: dict) -> tuple[str, dict]:
    call = message.get("call") or {}
    if not isinstance(call, dict):
        raise ValueError("Invalid call")
    call_id = _text(call.get("id", ""), maximum=150, minimum=4)
    assistant_id = _text(call.get("assistantId") or "", maximum=150)
    flags = feature_flags(oid)
    if not assistant_id or assistant_id != flags.get("provider_agent_id"):
        from app.runtime_routing import is_routed_worker_call
        if not is_routed_worker_call(oid,call):
            raise ValueError("Assistant does not belong to this workspace")
    return call_id, call


def _phone(value: str) -> str:
    if not value:
        return ""
    if not re.fullmatch(r"\+?[0-9 ()-]{7,28}", value):
        raise ValueError("Invalid callback phone format")
    digits = re.sub(r"\D", "", value)
    if not 7 <= len(digits) <= 15:
        raise ValueError("Invalid callback phone length")
    return value


def capture_customer_lead(oid: str, message: dict, args: dict) -> str:
    call_id, call = _call_context(oid, message)
    if not feature_flags(oid).get("voice_leads_enabled"):
        raise ValueError("Lead capture has not been enabled by this business")
    name = _text(args.get("caller_name", ""), maximum=100)
    interest = _text(args.get("interest", ""), maximum=200, minimum=2)
    explicit = _phone(_text(args.get("callback_phone", ""), maximum=28))
    # The provider-supplied caller ID is not automatically an authenticated identity.
    customer = call.get("customer") if isinstance(call.get("customer"), dict) else {}
    inbound = _phone(_text(customer.get("number", ""), maximum=28)) if not explicit else ""
    number = explicit or inbound
    if args.get("callback_requested") is not True:
        raise ValueError("The caller has not agreed to a callback")
    if not number:
        raise ValueError("Ask the caller for a callback number first")
    existing = api("call_insights", params={
        "organization_id": "eq." + oid, "provider_call_id": "eq." + call_id,
        "select": "id,lead_source,caller_name,caller_phone,product_interest", "limit": 1,
    })
    update = {
        "caller_phone": number, "product_interest": interest,
        "follow_up_needed": True, "follow_up_status": "todo",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if name:
        update["caller_name"] = name
    if existing:
        if existing[0].get("lead_source") != "human_reviewed":
            update["lead_source"] = "ai_suggested"
            update["lead_reason"] = "Caller explicitly requested a callback about: " + interest
        api("call_insights", "PATCH", params={
            "organization_id": "eq." + oid, "provider_call_id": "eq." + call_id,
        }, data=update, prefer="return=minimal")
    else:
        api("call_insights", "POST", params={
            "on_conflict": "organization_id,provider_call_id",
        }, data={
            "organization_id": oid, "provider_call_id": call_id,
            "assistant_id": feature_flags(oid).get("provider_agent_id"),
            "provider": "vapi", "direction": "inbound" if call.get("type") == "inboundPhoneCall" else "other",
            "provider_status": "in-progress", "lead_stage": "needs_review",
            "lead_source": "ai_suggested",
            "lead_reason": "Caller explicitly requested a callback about: " + interest,
            **update,
        }, prefer="resolution=merge-duplicates,return=minimal")
    # Exported in the background so a slow Google response can never delay the spoken reply.
    import threading
    from app.sheets import export_lead
    threading.Thread(target=export_lead, args=(oid, call_id, name, number, interest), daemon=True).start()
    return "Callback request saved for " + (name or "the caller") + " about " + interest + ". A team member has not yet contacted them."


def _slot(args: dict) -> tuple[datetime, datetime, int]:
    from app.calendar import Slot, check_slot
    raw = _text(args.get("start", ""), maximum=70, minimum=16)
    try:
        start = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        duration = int(args.get("duration_minutes"))
        if isinstance(args.get("duration_minutes"), bool):
            raise ValueError("Duration must be numeric")
        obj = Slot(start=start, duration_minutes=duration)
    except (ValueError, TypeError) as exc:
        raise ValueError("Provide an exact future date and time with timezone offset") from exc
    try:
        first, last = check_slot(obj)
    except HTTPException as exc:
        raise ValueError(str(exc.detail)) from exc
    return first, last, duration


def check_appointment_availability(oid: str, message: dict, args: dict) -> str:
    _call_context(oid, message)
    if not feature_flags(oid).get("voice_booking_enabled"):
        raise ValueError("Voice booking has not been enabled by this business")
    from app.calendar import google_token, free_busy, within_booking_hours, OUTSIDE_HOURS
    start, end, duration = _slot(args)
    if not within_booking_hours(oid, start, end):
        return OUTSIDE_HOURS + " No booking was made."
    available = free_busy(google_token(oid), start, end)
    when = start.strftime("%A, %B %d at %I:%M %p %z")
    return (("Available" if available else "Unavailable") + ": " + when
            + " for " + str(duration) + " minutes. This is NOT a booking confirmation.")


def _event_id(key: str) -> str:
    # Google Calendar supports a-z? Identifiers use base32hex; hexadecimal is a safe subset.
    return "cp" + hashlib.sha256(key.encode("ascii")).hexdigest()[:38]


def book_appointment(oid: str, message: dict, args: dict) -> str:
    call_id, _ = _call_context(oid, message)
    if not feature_flags(oid).get("voice_booking_enabled"):
        raise ValueError("Voice booking has not been enabled by this business")
    if args.get("confirmed") is not True:
        raise ValueError("Read the details aloud and obtain explicit caller agreement before booking")
    start, end, duration = _slot(args)
    name = _text(args.get("caller_name", ""), maximum=100, minimum=2)
    contact = _text(args.get("caller_contact", ""), maximum=100, minimum=3)
    service = _text(args.get("service", ""), maximum=150, minimum=2)
    if "@" in contact:
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", contact):
            raise ValueError("Invalid contact email")
    else:
        _phone(contact)
    return confirm_calendar_booking(oid, call_id, start, end, duration, name, contact, service)["message"]


def confirm_calendar_booking(oid, call_id, start, end, duration, name, contact, service):
    key = hashlib.sha256((oid + "|" + call_id + "|" + start.astimezone(timezone.utc).isoformat()
                           + "|" + str(duration) + "|" + name.casefold() + "|" + service.casefold()).encode()).hexdigest()
    from app.account import rpc
    from app.calendar import google_token, free_busy, within_booking_hours, OUTSIDE_HOURS

    reserved = rpc("reserve_voice_appointment", {
        "p_organization_id": oid, "p_idempotency_key": key,
        "p_caller_name": name, "p_caller_contact": contact, "p_service": service,
        "p_requested_time": start.isoformat(), "p_duration_minutes": duration,
        "p_provider_call_id": call_id,
    })
    if not isinstance(reserved, list) or len(reserved) != 1:
        raise ValueError("Could not reserve the appointment securely")
    row = reserved[0]
    status, rid = row.get("reservation_status"), row.get("reservation_id")
    if status == "conflict":
        return {"confirmed": False, "message": "That time has just been requested by another customer. Please ask for another time."}
    if status == "rejected":
        raise ValueError("That booking attempt previously failed; ask for another time")
    if status == "confirmed":
        prior = api("appointment_requests", params={
            "organization_id": "eq." + oid, "id": "eq." + str(rid),
            "select": "calendar_event_id,requested_time", "limit": 1,
        })
        if prior and prior[0].get("calendar_event_id"):
            return {"confirmed": True, "appointment_id": str(rid), "message": "Already confirmed: appointment " + str(rid) + " on " + (prior[0].get("requested_time") or start.isoformat())}
        raise ValueError("Appointment is marked confirmed but its event reference is missing")
    if status not in ("reserved", "requested") or not rid:
        raise ValueError("Unexpected booking reservation status")
    access = google_token(oid)
    eid = _event_id(key)
    url = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
    headers = {"Authorization": "Bearer " + access}
    # Deterministic ID makes provider retries safe. Check a previously created event first.
    try:
        with httpx.Client(timeout=12) as c:
            prior = c.get(url + "/" + eid, headers=headers)
            if prior.status_code == 200:
                event = prior.json()
                meta = ((event.get("extendedProperties") or {}).get("private") or {})
                if meta.get("callpilot_reservation_id") != str(rid):
                    raise ValueError("Calendar event ID belongs to a different reservation")
                provider_confirmed = True
            elif prior.status_code == 404:
                inside_hours = within_booking_hours(oid, start, end)
                if not inside_hours or not free_busy(access, start, end):
                    api("appointment_requests", "PATCH", params={
                        "organization_id": "eq." + oid, "id": "eq." + str(rid),
                        "status": "eq.requested",
                    }, data={"status": "rejected"}, prefer="return=minimal")
                    if not inside_hours:
                        return {"confirmed": False, "message": OUTSIDE_HOURS + " No booking was made; offer another time."}
                    return {"confirmed": False, "message": "The calendar is busy at that time. No booking was made; offer another time."}
                event = {
                    "id": eid, "summary": service,
                    "description": "CallPilot booking; callback: " + contact + "; customer: " + name,
                    "start": {"dateTime": start.isoformat()},
                    "end": {"dateTime": end.isoformat()},
                    "extendedProperties": {"private": {
                        "callpilot_reservation_id": str(rid), "callpilot_org": oid,
                    }},
                }
                resp = c.post(url, headers=headers, json=event)
                if resp.status_code not in (200, 201):
                    if resp.status_code == 409:
                        raise ValueError("Calendar event conflict; booking status needs reconciliation")
                    raise ValueError("Calendar could not confirm this appointment")
                provider_confirmed = bool(resp.json().get("id") == eid)
            else:
                raise ValueError("Calendar event lookup unavailable; booking is not confirmed")
    except httpx.RequestError as exc:
        raise ValueError("Google Calendar could not be reached; booking not yet confirmed") from exc
    if not provider_confirmed:
        raise ValueError("Google Calendar returned no matching event confirmation")
    api("appointment_requests", "PATCH", params={
        "organization_id": "eq." + oid, "id": "eq." + str(rid),
        "status": "eq.requested",
    }, data={"status": "confirmed", "calendar_event_id": eid}, prefer="return=minimal")
    return {
        "confirmed": True, "appointment_id": str(rid),
        "message": ("Confirmed Google Calendar appointment " + str(rid) + " on " + start.isoformat()
                    + " for " + service + ". Caller contact: " + contact + "."),
    }


def _catalog_match(oid: str, item: str) -> dict | None:
    items=api("catalog_items",params={
        "organization_id":"eq."+oid,"active":"eq.true",
        "select":"id,sku,name,description,unit_price_cents,stock_quantity,use_shopify_inventory",
        "limit":200,
    })
    query=item.casefold().strip()
    exact=[p for p in items if p["sku"].casefold()==query or p["name"].casefold()==query]
    if len(exact)==1:return exact[0]
    if len(exact)>1:raise ValueError("Multiple catalog items matched. Ask for the SKU.")
    candidates=[p for p in items if query in p["name"].casefold() and len(query)>=4]
    if len(candidates)==1:return candidates[0]
    if candidates:raise ValueError("Several catalog items match. Ask for the SKU.")
    return None


def _product_stock(oid: str, product: dict) -> tuple[int | None, str]:
    if product.get("use_shopify_inventory"):
        from app.shopify_connection import lookup_inventory
        try:
            variant=lookup_inventory(UUID(oid), product["sku"])
        except HTTPException:
            variant=None
        quantity=variant.get("quantity") if variant else None
        if type(quantity) is int:
            available=max(0,quantity)
            return available,"Live Shopify stock: "+str(available)+" units available at lookup time. "
        return None,"Live Shopify stock could not be verified. Offer staff assistance. "
    quantity=product.get("stock_quantity")
    return quantity,("Stock on record: "+str(quantity)+" units. " if quantity is not None
                     else "Stock availability is not configured. ")


def lookup_catalog_item(oid: str,message: dict,args: dict) -> str:
    _call_context(oid,message)
    flags=feature_flags(oid)
    if not flags.get("voice_catalog_enabled"):
        raise ValueError("The business has not enabled catalog lookup")
    item=_text(args.get("item",""),maximum=160,minimum=2)
    product=_catalog_match(oid,item)
    if not product:
        return "I cannot find that product in the business's approved catalog. Ask for a SKU or offer a staff follow-up."
    _,availability=_product_stock(oid,product)
    price=product.get("unit_price_cents")
    pricing=(" Approved unit price: $"+format(price/100,".2f")+" USD. "
             if flags.get("allow_price_quotes") and price is not None else
             " Price must be confirmed by staff. ")
    description=_text(product.get("description") or "",maximum=1200)[:250]
    return ("SKU "+product["sku"]+": "+product["name"]+". "+description+". "
            +availability+pricing+"Do not call this a confirmed order.")


def submit_order_request(oid: str,message: dict,args: dict) -> str:
    call_id,_=_call_context(oid,message)
    flags=feature_flags(oid)
    if not flags.get("voice_order_requests_enabled"):
        raise ValueError("Pending order requests are not enabled")
    if args.get("confirmed") is not True:
        raise ValueError("Get caller agreement before submitting an order request")
    sku=_text(args.get("sku",""),maximum=60,minimum=2).upper()
    qty=args.get("quantity")
    if not isinstance(qty,int) or isinstance(qty,bool) or not 1<=qty<=100000:
        raise ValueError("Quantity must be a positive whole number")
    name=_text(args.get("caller_name",""),maximum=100,minimum=2)
    contact=_text(args.get("caller_contact",""),maximum=100,minimum=3)
    if "@" in contact:
        if not re.fullmatch(r"[^@ ]+@[^@ ]+[.][^@ ]+",contact):
            raise ValueError("Invalid contact email")
    else:
        _phone(contact)
    product=_catalog_match(oid,sku)
    if not product or product.get("sku")!=sku:
        raise ValueError("Product SKU is not in the approved catalog")
    key=hashlib.sha256((oid+"|"+call_id+"|"+sku+"|"+str(qty)+"|"+contact.casefold()).encode()).hexdigest()
    existing=api("order_requests",params={"organization_id":"eq."+oid,
        "idempotency_key":"eq."+key,"select":"id,status","limit":1})
    if existing:
        return "Order request already submitted with reference "+existing[0]["id"]+". Do not submit it again."
    stock,_=_product_stock(oid,product)
    if stock is not None and qty>stock:
        return "The requested quantity exceeds the available stock shown. No order request was submitted. Offer human assistance."
    rows=api("order_requests","POST",params={"on_conflict":"organization_id,idempotency_key"},
        data={"organization_id":oid,"provider_call_id":call_id,
              "idempotency_key":key,"item_id":product["id"],"sku":sku,
              "quantity":qty,"caller_name":name,"caller_contact":contact,"status":"requested"},
        prefer="resolution=ignore-duplicates,return=representation")
    if rows:
        return "Pending order request "+str(rows[0]["id"])+" saved for "+str(qty)+" units of "+product["name"]+". No payment or final order was placed. Staff approval is required."
    found=api("order_requests",params={"organization_id":"eq."+oid,
        "idempotency_key":"eq."+key,"select":"id","limit":1})
    if not found:raise ValueError("Could not verify the order request was recorded")
    return "Order request "+str(found[0]["id"])+" already submitted. Do not submit it again."


def resolve_call_menu(oid: str, message: dict, args: dict) -> str:
    _call_context(oid, message)
    from app.call_menu import resolve

    return resolve(feature_flags(oid), _text(args.get("spoken_choice", ""), maximum=80))


def execute_voice_action(oid: str, message: dict, name: str, args: dict) -> str:
    if not isinstance(args, dict):
        raise ValueError("Invalid action arguments")
    handlers = {
        "resolve_call_menu": resolve_call_menu,
        "capture_customer_lead": capture_customer_lead,
        "check_appointment_availability": check_appointment_availability,
        "book_appointment": book_appointment,
        "lookup_catalog_item": lookup_catalog_item,
        "submit_order_request": submit_order_request,
    }
    if name not in handlers:
        if isinstance(name, str) and name.startswith("api_"):
            from app.custom_apis import run_by_tool_name
            call_id, _ = _call_context(oid, message)
            return run_by_tool_name(oid, name, call_id, args)
        raise ValueError("Unknown voice action")
    return handlers[name](oid, message, args)
