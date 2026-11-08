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
        log.warning("lobbystack_failed method=%s path=%s status=%s", method, path, response.status_code)
        raise HTTPException(502, "The receptionist service rejected the request (HTTP %s)." % response.status_code)
    return response.json() if response.content else {}


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


class Provision(BaseModel):
    website_url: str | None = Field(default=None, max_length=500)


class Website(BaseModel):
    url: str = Field(min_length=4, max_length=500)


class Reschedule(BaseModel):
    starts_at: str = Field(min_length=10, max_length=40)


def writer(oid, user):
    UUID(oid)
    membership(oid, user, WRITERS)


@router.post("/api/account/organizations/{oid}/lobbystack/provision")
def provision(oid: str, body: Provision, user=Depends(current_user)):
    writer(oid, user)
    if link(oid):
        return public_status(oid)
    org = org_or_404(oid)
    request = {"name": org["name"], "timezone": org.get("timezone") or "America/New_York", "locale": "en"}
    if body.website_url:
        request["website_url"] = body.website_url
    # Same workspace always sends the same key, so a retry cannot create a second business.
    created = call("POST", "/businesses", body=request, idem="callpilot-" + oid)["data"]
    base = os.getenv("PUBLIC_BASE_URL", "https://callpilot-internal.onrender.com").rstrip("/")
    hook = call("POST", "/webhooks", business=created["id"], body={
        "url": base + "/webhooks/lobbystack", "events": list(EVENTS), "description": "CallPilot dashboard"})["data"]
    api("lobbystack_businesses", "POST", params={"on_conflict": "organization_id"}, prefer="resolution=ignore-duplicates", data={
        "organization_id": oid, "business_id": created["id"], "webhook_id": hook["id"],
        "webhook_secret_ciphertext": cipher().encrypt(hook["secret"].encode()).decode()})
    return public_status(oid)


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
    return {name: call("GET", path, business=biz).get("data", []) for name, path in
            (("sources", "/knowledge/sources"), ("services", "/services"), ("rules", "/rules"), ("knowledge", "/knowledge"))}


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
