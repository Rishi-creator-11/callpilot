"""Hosted LobbyStack receptionist connection.

One CallPilot workspace maps to one LobbyStack business. CallPilot owns setup and the dashboard; LobbyStack
runs the receptionist (website import, knowledge, voice, calendar booking) and is the source of truth for
calls, appointments and messages. The partner key stays on the server. Webhooks follow Standard Webhooks.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.account import current_user, membership
from app.calendar import cipher
from app.cloud import api, org_or_404
from app.menu import answers, parse

router = APIRouter(tags=["lobbystack"])
log = logging.getLogger("callpilot")
BASE = os.getenv("LOBBYSTACK_API_BASE", "https://app.lobbystack.com/api/v1")
EVENTS = ("call.completed", "appointment.booked", "appointment.rescheduled", "appointment.cancelled", "message.taken")
TOLERANCE = 300
WRITERS = ("owner", "admin")


def call(method, path, *, business=None, body=None, params=None, idem=None):
    key = os.getenv("LOBBYSTACK_PLATFORM_KEY", "")
    if not key:
        raise HTTPException(503, "The receptionist service is not connected yet. The platform operator must add the LobbyStack key.")
    headers = {"Authorization": "Bearer " + key}
    if business:
        headers["LobbyStack-Business"] = business
    if idem:
        headers["Idempotency-Key"] = idem
    try:
        with httpx.Client(timeout=25) as client:
            response = client.request(method, BASE + path, headers=headers, json=body, params=params)
    except httpx.RequestError:
        raise HTTPException(503, "The receptionist service is unavailable. Try again shortly.")
    if response.status_code >= 400:
        code = ""
        try:
            err = response.json().get("error") or {}
            code = str(err.get("code") or err.get("message") or "")[:80] if isinstance(err, dict) else str(err)[:80]
        except (ValueError, AttributeError):
            pass
        log.warning("lobbystack_failed method=%s path=%s status=%s code=%s", method, path.split("?")[0], response.status_code, code)
        # Provider codes are short fixed identifiers, safe to show the workspace owner for support.
        raise HTTPException(502, "The receptionist service rejected the request (HTTP %s%s)." % (response.status_code, ", " + code if code else ""))
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError:
        # Some successful calls, such as deletes, return no JSON body.
        return {}


def listing(path, business, limit_pages=20):
    """Collect every page of a list endpoint; the API returns 25 rows at a time with a cursor."""
    rows, cursor = [], None
    for _ in range(limit_pages):
        page = call("GET", path, business=business, params={"cursor": cursor} if cursor else None)
        rows += page.get("data", [])
        cursor = page.get("next_cursor")
        if not page.get("has_more") or not cursor:
            break
    return rows


def link(oid):
    rows = api("lobbystack_businesses", params={"organization_id": "eq." + oid, "select": "*"})
    return rows[0] if rows else None


def require_link(oid):
    saved = link(oid)
    if not saved:
        raise HTTPException(409, "Set up the receptionist for this workspace first.")
    return saved


def verify_signature(secret, headers, raw, now=None):
    """Standard Webhooks: HMAC-SHA256 over id.timestamp.body, key is the base64 part after whsec_."""
    msg_id, stamp, given = (headers.get("webhook-id"), headers.get("webhook-timestamp"), headers.get("webhook-signature"))
    if not (msg_id and stamp and given and secret.startswith("whsec_")):
        return False
    try:
        if abs((now or time.time()) - int(stamp)) > TOLERANCE:
            return False
        key = base64.b64decode(secret[6:])
    except ValueError:
        return False
    digest = hmac.new(key, msg_id.encode() + b"." + stamp.encode() + b"." + raw, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode()
    return any(hmac.compare_digest(part.split(",", 1)[1], expected) for part in given.split() if part.startswith("v1,"))


def store_event(oid, event):
    """Insert once per event id; a retry returns False and creates nothing."""
    data = event.get("data") or {}
    rows = api("lobbystack_events", "POST", params={"on_conflict": "event_id"}, prefer="resolution=ignore-duplicates,return=representation", data={
        "event_id": event["id"], "organization_id": oid, "event_type": event["type"],
        "resource_id": data.get("id"), "payload": event, "occurred_at": event.get("created_at")})
    return bool(rows)


HUMAN_STYLE = """You are the person who answers the phone for this business. You are a real, warm, slightly casual human, not a call center script.

How you speak:
- Talk the way people talk on the phone: contractions, short sentences, an occasional light filler like 'um', 'so', 'yeah', 'let me see'. Never sound formal or robotic.
- Keep most replies under fifteen words. One thought, then stop and let the caller talk.
- Match the caller's energy. If they are quick and casual, be quick. If they are unsure, slow down and be reassuring.
- Always react first, then answer. Examples: 'Oh nice, yeah we can do that.' 'Gotcha.' 'Sure thing.' 'Mm, let me check.'
- When you look something up, think out loud briefly: 'Let me see what we've got tomorrow... okay, so I've got eleven or eleven thirty.'
- Say numbers and times the way people do: 'eleven thirty', 'fifty bucks', 'about half an hour'. Never read out dates like 2026-11-11.
- Never say 'I'd be happy to assist', 'certainly', 'as an AI', 'one moment please while I process', or 'is there anything else I can help you with'. Say 'Anything else?' instead.
- Never repeat yourself. If you already asked something, do not ask it the same way again.
- If the caller interrupts, stop and listen, then respond to what they said.
- If there is silence, say 'No rush' or 'Take your time', never 'Are you still there?'.

Example of the right feel:
Caller: 'Hey, can I book something tomorrow?'
You: 'Yeah, for sure. What time works for you?'
Caller: 'Anytime in the morning.'
You: 'Let me see... I've got eleven or eleven thirty.'
Caller: 'Eleven thirty.'
You: 'Perfect. Can I get your name and number?'
Caller: 'It's Sam, nine one four...'
You: 'Thanks Sam, you're all set for eleven thirty tomorrow. Anything else?'

Rules you never break: only say a booking is confirmed after the booking system confirms it. Quote prices and durations only from the business's saved services. If you don't know something, say so simply and offer to take a message. Do not say you are an AI unless asked directly."""


def clear_injected_style():
    """One-time clean-up: remove only the exact prompt CallPilot used to inject into new businesses.

    Owners' own edits never match it, so they are left alone. Safe to run on every start."""
    if not os.getenv("LOBBYSTACK_PLATFORM_KEY"):
        return 0
    cleared = 0
    for row in api("lobbystack_businesses", params={"select": "business_id"}):
        try:
            current = (call("GET", "/receptionist", business=row["business_id"]).get("data") or {})
            if str(current.get("voice_instructions") or "").strip() == HUMAN_STYLE.strip():
                call("PATCH", "/receptionist", business=row["business_id"], body={"voice_instructions": None})
                cleared += 1
        except HTTPException as exc:
            log.warning("lobbystack_style_cleanup_failed business=%s detail=%s", row.get("business_id"), exc.detail)
    return cleared


class Provision(BaseModel):
    website_url: str | None = Field(default=None, max_length=500)


class Website(BaseModel):
    url: str = Field(min_length=4, max_length=500)


class ServiceIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    duration_minutes: int = Field(ge=5, le=480)
    price_text: str = Field(min_length=1, max_length=40)
    description: str | None = Field(default=None, max_length=300)


class Hours(BaseModel):
    day: str = Field(pattern="^(sunday|monday|tuesday|wednesday|thursday|friday|saturday)$")
    open: str = Field(pattern="^[0-2][0-9]:[0-5][0-9]$")
    close: str = Field(pattern="^[0-2][0-9]:[0-5][0-9]$")


class Profile(BaseModel):
    """What the owner reviewed and approved. Prices are only saved here, never guessed by the receptionist."""
    services: list[ServiceIn] = Field(max_length=40)
    hours: list[Hours] = Field(default_factory=list, max_length=7)
    booking_mode: str = Field(default="request", pattern="^(off|request|instant)$")


class Booking(BaseModel):
    service_id: str = Field(min_length=8, max_length=64)
    starts_at: str = Field(min_length=10, max_length=40)
    contact_name: str = Field(min_length=1, max_length=80)
    contact_phone: str = Field(min_length=7, max_length=20)
    key: str = Field(min_length=8, max_length=64)


class Style(BaseModel):
    """Only the fields that change how the receptionist sounds. Booking and transfer settings stay out of reach."""
    greeting: str | None = Field(default=None, max_length=400)
    tone: str | None = Field(default=None, max_length=300)
    voice: str | None = Field(default=None, pattern="^[a-z]{3,12}$")
    voice_instructions: str | None = Field(default=None, max_length=3000)
    business_name: str | None = Field(default=None, min_length=2, max_length=120)


class Fact(BaseModel):
    question: str = Field(min_length=5, max_length=300)
    answer: str = Field(min_length=3, max_length=1500)


class Menu(BaseModel):
    title: str = Field(min_length=2, max_length=60)
    text: str = Field(default="", max_length=30000)
    facts: list[Fact] = Field(default_factory=list, max_length=20)


class Reschedule(BaseModel):
    starts_at: str = Field(min_length=10, max_length=40)


def writer(oid, user):
    UUID(oid)
    membership(oid, user, WRITERS)


@router.post("/api/account/organizations/{oid}/lobbystack/provision")
def provision(oid: str, body: Provision, user=Depends(current_user)):
    UUID(oid)
    role = membership(oid, user, WRITERS)
    if link(oid):
        return public_status(oid)
    org = org_or_404(oid)
    request = {"name": org["name"], "timezone": org.get("timezone") or "America/New_York", "locale": "en", "access_level": "full"}
    if body.website_url:
        request["website_url"] = body.website_url
    # The workspace owner is invited to LobbyStack's own dashboard, where its onboarding, knowledge,
    # services, calendars and phone number are managed as LobbyStack ships them. Nothing is preset here.
    owner_email = (user.get("email") or "").strip().lower() if role == "owner" else ""
    if owner_email:
        request["owner_email"] = owner_email
    # Same workspace always sends the same key, so a retry cannot create a second business.
    created = call("POST", "/businesses", body=request, idem="callpilot-v2-" + oid)["data"]
    base = os.getenv("PUBLIC_BASE_URL", "https://callpilot-internal.onrender.com").rstrip("/")
    hook = call("POST", "/webhooks", business=created["id"], body={
        "url": base + "/webhooks/lobbystack", "events": list(EVENTS), "description": "CallPilot dashboard"})["data"]
    row = {"organization_id": oid, "business_id": created["id"], "webhook_id": hook["id"],
           "webhook_secret_ciphertext": cipher().encrypt(hook["secret"].encode()).decode()}
    if owner_email:
        row["owner_invited_email"] = owner_email
        row["owner_invited_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    api("lobbystack_businesses", "POST", params={"on_conflict": "organization_id"}, prefer="resolution=ignore-duplicates", data=row)
    return public_status(oid)


_dashboard_cache = {"at": 0.0, "url": ""}


def dashboard_url():
    """Where owners sign in to LobbyStack: the partner's own address, else LobbyStack's."""
    configured = os.getenv("LOBBYSTACK_DASHBOARD_URL", "").strip().rstrip("/")
    if configured.startswith("https://"):
        return configured
    if _dashboard_cache["url"] and time.time() - _dashboard_cache["at"] < 600:
        return _dashboard_cache["url"]
    try:
        url = str((call("GET", "/partner").get("data") or {}).get("dashboard_url") or "").rstrip("/")
        if url.startswith("https://"):
            _dashboard_cache.update(at=time.time(), url=url)
            return url
    except HTTPException:
        pass
    return "https://app.lobbystack.com"


@router.get("/api/account/organizations/{oid}/lobbystack/dashboard")
def hosted_dashboard(oid: str, user=Depends(current_user)):
    UUID(oid)
    role = membership(oid, user)
    saved = require_link(oid)
    invited = saved.get("owner_invited_email")
    # The invited address is only for the people who run the workspace, never read-only members.
    return {"dashboard_url": dashboard_url(), "business_id": saved["business_id"],
            "owner_invited_email": invited if role in WRITERS else None, "owner_invited": bool(invited)}


def public_status(oid):
    saved = link(oid)
    if not saved:
        return {"connected": False}
    return {"connected": True, "business_id": saved["business_id"], "calendar": saved["calendar_status"],
            "webhook_verified": bool(saved["webhook_verified_at"])}


@router.get("/api/account/organizations/{oid}/lobbystack")
def status(oid: str, user=Depends(current_user)):
    UUID(oid)
    membership(oid, user)
    return public_status(oid)


@router.post("/api/account/organizations/{oid}/lobbystack/website")
def import_website(oid: str, body: Website, user=Depends(current_user)):
    writer(oid, user)
    saved = require_link(oid)
    return call("POST", "/knowledge/sources", business=saved["business_id"], body={"type": "website", "url": body.url})


@router.get("/api/account/organizations/{oid}/lobbystack/review")
def review(oid: str, user=Depends(current_user)):
    UUID(oid)
    membership(oid, user)
    biz = require_link(oid)["business_id"]
    return {name: listing(path, biz) for name, path in
            (("sources", "/knowledge/sources"), ("services", "/services"), ("rules", "/rules"), ("knowledge", "/knowledge"))}


@router.post("/api/account/organizations/{oid}/lobbystack/profile")
def apply_profile(oid: str, body: Profile, user=Depends(current_user)):
    """Save reviewed services, prices, hours and booking mode through LobbyStack's native settings. Safe to repeat."""
    writer(oid, user)
    biz = require_link(oid)["business_id"]
    entries = listing("/knowledge", biz)
    existing, duplicates = {}, []
    for entry in entries:
        # The API lists an FAQ with its question as the title.
        question = entry.get("title") or entry.get("question")
        if question in existing:
            duplicates.append(entry["id"])
        elif question:
            existing[question] = entry["id"]
        if not question:
            continue
    removed = 0
    for entry_id in duplicates:
        try:
            call("DELETE", "/knowledge/%s" % entry_id, business=biz)
            removed += 1
        except HTTPException as exc:
            # Duplicates are harmless clutter, so one failed delete must not stop the price update.
            log.warning("lobbystack_duplicate_delete_failed detail=%s", exc.detail)
            break
    have = {item["name"].lower(): item for item in listing("/services", biz)}
    made = []
    for item in body.services:
        # The price and time live on the service itself, so the receptionist sees them with the service it is booking.
        detail = "%s, about %s minutes%s" % (item.price_text, item.duration_minutes, ". " + item.description if item.description else "")
        saved = have.get(item.name.lower())
        if not saved:
            saved = call("POST", "/services", business=biz, idem="svc-%s-%s" % (oid, hashlib.sha1(item.name.lower().encode()).hexdigest()[:16]), body={
                "name": item.name, "duration_minutes": item.duration_minutes, "description": detail})["data"]
        elif saved.get("description") != detail:
            saved = call("PATCH", "/services/%s" % saved["id"], business=biz, body={"description": detail})["data"]
        question = "How much is a %s and how long does it take?" % item.name.lower()
        if question not in existing:
            call("POST", "/knowledge", business=biz, body={"type": "faq", "question": question, "answer": "A %s is %s and takes about %s minutes." % (
                item.name.lower(), item.price_text, item.duration_minutes)})
        made.append({"id": saved["id"], "name": saved["name"], "duration_minutes": saved["duration_minutes"], "price": item.price_text})
    if body.services:
        menu = "; ".join("%s %s (%s min)" % (i.name, i.price_text, i.duration_minutes) for i in body.services)
        price_list = "Here is our full price list: " + menu + "."
        key = "What are your prices?"
        if key in existing:
            call("PATCH", "/knowledge/%s" % existing[key], business=biz, body={"content": price_list})
        else:
            call("POST", "/knowledge", business=biz, body={"type": "faq", "question": key, "answer": price_list})
    if body.hours:
        call("PATCH", "/business", business=biz, body={"hours": [h.model_dump() for h in body.hours]})
    call("PATCH", "/receptionist", business=biz, body={"booking_mode": body.booking_mode})
    return {"services": made, "booking_mode": body.booking_mode, "duplicates_removed": removed, "duplicates_left": len(duplicates) - removed}


@router.post("/api/account/organizations/{oid}/lobbystack/demo")
def make_demo(oid: str, body: Website, user=Depends(current_user)):
    """Create a no-sign-in browser demo of the receptionist for the owner to try. The link is shown once."""
    writer(oid, user)
    org = org_or_404(oid)
    demo = call("POST", "/partner/demos", body={"name": org["name"], "website_url": body.url,
                                                "timezone": org.get("timezone") or "America/New_York", "access_level": "full"})["data"]
    return {"id": demo["id"], "status": demo["status"], "url": demo["url"]}


@router.get("/api/account/organizations/{oid}/lobbystack/style")
def get_style(oid: str, user=Depends(current_user)):
    UUID(oid)
    membership(oid, user)
    data = call("GET", "/receptionist", business=require_link(oid)["business_id"]).get("data", {})
    return {k: data.get(k) for k in ("greeting", "tone", "voice", "voice_instructions", "booking_mode", "summary")}


@router.post("/api/account/organizations/{oid}/lobbystack/style")
def set_style(oid: str, body: Style, user=Depends(current_user)):
    writer(oid, user)
    changes = body.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(422, "Nothing to change.")
    biz = require_link(oid)["business_id"]
    name = changes.pop("business_name", None)
    if name:
        call("PATCH", "/business", business=biz, body={"name": name})
    if changes:
        call("PATCH", "/receptionist", business=biz, body=changes)
    return {"updated": sorted(changes) + (["business_name"] if name else [])}


SUMMARY_FIELDS = ("id", "channel", "status", "outcome", "summary", "duration_seconds", "started_at", "ended_at", "recording_available")


@router.get("/api/account/organizations/{oid}/lobbystack/calls/{call_id}")
def call_detail(oid: str, call_id: str, user=Depends(current_user)):
    """Full record with transcript and caller details, for owners and admins only. Recording links are never passed on."""
    writer(oid, user)
    data = call("GET", "/calls/%s" % UUID(call_id), business=require_link(oid)["business_id"]).get("data", {})
    keep = SUMMARY_FIELDS + ("caller_name", "caller_phone", "end_reason", "transcript")
    return {k: data.get(k) for k in keep}


@router.get("/api/account/organizations/{oid}/lobbystack/calls")
def calls(oid: str, user=Depends(current_user)):
    """Summaries only, with no caller phone numbers or transcripts, so any member can see what happened."""
    UUID(oid)
    membership(oid, user)
    rows = call("GET", "/calls", business=require_link(oid)["business_id"], params={"limit": 20}).get("data", [])
    return {"calls": [{k: row.get(k) for k in SUMMARY_FIELDS} for row in rows]}


@router.post("/api/account/organizations/{oid}/lobbystack/menu")
def import_menu(oid: str, body: Menu, user=Depends(current_user)):
    """Save pasted menu text as one exact answer per dish and one spoken list per section. Safe to repeat."""
    writer(oid, user)
    biz = require_link(oid)["business_id"]
    sections = parse(body.text) if body.text.strip() else []
    pairs = answers(body.title, sections) + [(fact.question, fact.answer) for fact in body.facts]
    if not pairs:
        raise HTTPException(422, "No dishes with prices were found. Paste each dish's name, then its price on its own line.")
    if len(pairs) > 120:
        raise HTTPException(422, "That menu is too long to import at once. Paste one section at a time.")
    existing = {}
    for entry in listing("/knowledge", biz):
        if entry.get("title") and entry["title"] not in existing:
            existing[entry["title"]] = entry
    added = updated = removed = 0
    wanted = {question for question, _ in pairs}
    tail = " on the %s?" % body.title.lower()
    section_mark = "do you have on the %s?" % body.title.lower()
    # Re-importing a menu replaces it: answers that belong to this menu but are no longer in it are removed.
    for title_, entry in existing.items():
        if title_ not in wanted and sections and (title_.endswith(tail) or section_mark in title_):
            call("DELETE", "/knowledge/%s" % entry["id"], business=biz)
            removed += 1
    for question, answer in pairs:
        found = existing.get(question)
        if found is None:
            call("POST", "/knowledge", business=biz, body={"type": "faq", "question": question, "answer": answer})
            added += 1
        elif found.get("content") != answer:
            call("PATCH", "/knowledge/%s" % found["id"], business=biz, body={"content": answer})
            updated += 1
    return {"sections": [{"name": sec["name"], "dishes": len(sec["items"])} for sec in sections], "added": added, "updated": updated, "removed": removed}


@router.post("/api/account/organizations/{oid}/lobbystack/calendar/connect")
def connect_calendar(oid: str, user=Depends(current_user)):
    writer(oid, user)
    return call("POST", "/calendars/google/connect", business=require_link(oid)["business_id"])["data"]


@router.get("/api/account/organizations/{oid}/lobbystack/calendar")
def calendar_state(oid: str, user=Depends(current_user)):
    writer(oid, user)
    saved = require_link(oid)
    items = call("GET", "/calendars", business=saved["business_id"]).get("data", [])
    state = "connected" if items else "not_connected"
    if state != saved["calendar_status"]:
        api("lobbystack_businesses", "PATCH", params={"organization_id": "eq." + oid}, data={"calendar_status": state})
    return {"status": state, "calendars": items}


@router.get("/api/account/organizations/{oid}/lobbystack/availability")
def availability(oid: str, service_id: str, start_date: str, end_date: str | None = None, user=Depends(current_user)):
    UUID(oid)
    membership(oid, user)
    params = {"service_id": service_id, "start_date": start_date, **({"end_date": end_date} if end_date else {})}
    return call("GET", "/availability", business=require_link(oid)["business_id"], params=params)


@router.get("/api/account/organizations/{oid}/lobbystack/appointments/{appointment_id}")
def appointment(oid: str, appointment_id: str, user=Depends(current_user)):
    UUID(oid)
    membership(oid, user)
    return call("GET", "/appointments/%s" % UUID(appointment_id), business=require_link(oid)["business_id"])


@router.post("/api/account/organizations/{oid}/lobbystack/appointments")
def book(oid: str, body: Booking, user=Depends(current_user)):
    """Book through LobbyStack, which checks the calendar and returns an error if the slot is taken."""
    writer(oid, user)
    return call("POST", "/appointments", business=require_link(oid)["business_id"], idem="book-%s-%s" % (oid, body.key),
                body={"service_id": body.service_id, "starts_at": body.starts_at, "contact_name": body.contact_name,
                      "contact_phone": body.contact_phone})


@router.post("/api/account/organizations/{oid}/lobbystack/appointments/{appointment_id}/cancel")
def cancel(oid: str, appointment_id: str, user=Depends(current_user)):
    writer(oid, user)
    return call("POST", "/appointments/%s/cancel" % UUID(appointment_id), business=require_link(oid)["business_id"])


@router.post("/api/account/organizations/{oid}/lobbystack/appointments/{appointment_id}/reschedule")
def reschedule(oid: str, appointment_id: str, body: Reschedule, user=Depends(current_user)):
    writer(oid, user)
    return call("POST", "/appointments/%s/reschedule" % UUID(appointment_id), business=require_link(oid)["business_id"],
                body={"starts_at": body.starts_at})


@router.get("/api/account/organizations/{oid}/lobbystack/activity")
def activity(oid: str, user=Depends(current_user)):
    UUID(oid)
    membership(oid, user)
    rows = api("lobbystack_events", params={"organization_id": "eq." + oid, "order": "received_at.desc", "limit": "50",
                                            "select": "event_id,event_type,resource_id,payload,received_at"})
    return {"events": rows}


@router.post("/webhooks/lobbystack")
async def receive(request: Request):
    raw = await request.body()
    if len(raw) > 262_144:
        raise HTTPException(413, "Too large")
    try:
        event = json.loads(raw)
        biz = str(event["business_id"])
        event_id, kind = str(event["id"]), str(event["type"])
    except (ValueError, KeyError, TypeError):
        raise HTTPException(400, "Invalid event")
    rows = api("lobbystack_businesses", params={"business_id": "eq." + biz, "select": "*"})
    # Unknown business and bad signature look the same to the sender.
    if not rows:
        raise HTTPException(401, "Invalid signature")
    saved = rows[0]
    try:
        secret = cipher().decrypt(saved["webhook_secret_ciphertext"].encode()).decode()
    except Exception:
        raise HTTPException(503, "Webhook secret unavailable")
    if not verify_signature(secret, request.headers, raw):
        raise HTTPException(401, "Invalid signature")
    if kind not in EVENTS and kind != "contact.created":
        return {"ok": True, "ignored": True}
    event["id"] = event_id
    fresh = store_event(saved["organization_id"], event)
    if fresh and not saved["webhook_verified_at"]:
        api("lobbystack_businesses", "PATCH", params={"organization_id": "eq." + saved["organization_id"]},
            data={"webhook_verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    return {"ok": True, "duplicate": not fresh}
