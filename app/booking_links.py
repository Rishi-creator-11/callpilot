"""Expiring single-use appointment links for customers invited by workspace staff."""

import hmac
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from app.account import current_user, membership, rpc
from app.booking_hours import OUTSIDE_HOURS, within_booking_hours
from app.calendar import Slot, check_slot, connection, free_busy, google_token
from app.cloud import api, org_or_404
from app.common import public_base
from app.demo_links import PRIVATE_HEADERS, signature
from app.voice_actions import confirm_calendar_booking

router = APIRouter(tags=["appointment-links"])
PATH = "/api/account/organizations/{oid}/calendar/booking-links"
TABLE = "appointment_booking_links"
PURPOSE = b"appointment-booking"
MANAGERS = ("owner", "admin")
TEMPLATE = Path(__file__).resolve().parent.parent / "static" / "book-appointment.html"


class NewLink(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    service: str = Field(min_length=2, max_length=150)
    duration_minutes: int = Field(ge=15, le=120)
    confirm: bool


class RequestedTime(BaseModel):
    start: datetime


class Booking(RequestedTime):
    model_config = ConfigDict(str_strip_whitespace=True)
    name: str = Field(min_length=2, max_length=100)
    contact: str = Field(min_length=3, max_length=100)
    confirm: bool


def link_url(link_id: UUID) -> str:
    return public_base() + "/book#" + link_id.hex + "." + signature(link_id, PURPOSE)


def authorized_link(authorization: str | None = Header(default=None)) -> dict:
    raw = authorization or ""
    match = re.fullmatch(r"Bearer ([a-f0-9]{32})\.([a-f0-9]{64})", raw)
    if match:
        link_id = UUID(hex=match[1])
        if hmac.compare_digest(match[2], signature(link_id, PURPOSE)):
            rows = api(TABLE, params={"id": "eq." + str(link_id), "status": "neq.revoked",
                "expires_at": "gt." + datetime.now(timezone.utc).isoformat(),
                "select": "id,organization_id,service,duration_minutes,status,checks,attempts,appointment_id", "limit": 1})
            if rows:
                return rows[0]
    raise HTTPException(404, "This appointment link is invalid, expired or revoked.", headers=PRIVATE_HEADERS)


@router.get(PATH)
def list_booking_links(oid: UUID, response: Response, user=Depends(current_user)):
    workspace = str(oid)
    membership(workspace, user, MANAGERS)
    response.headers.update(PRIVATE_HEADERS)
    rows = api(TABLE, params={"organization_id": "eq." + workspace,
        "select": "id,service,duration_minutes,status,expires_at,attempts", "order": "created_at.desc", "limit": 20})
    now = datetime.now(timezone.utc)
    for row in rows:
        active = datetime.fromisoformat(row["expires_at"]) > now
        if not active and row["status"] == "open":
            row["status"] = "expired"
        row["url"] = link_url(UUID(row["id"])) if active and row["status"] in ("open", "pending") else None
    return rows


@router.post(PATH)
def create_booking_link(oid: UUID, body: NewLink, response: Response, user=Depends(current_user)):
    workspace = str(oid)
    membership(workspace, user, MANAGERS)
    if not body.confirm:
        raise HTTPException(422, "Confirm that the recipient may book this service.")
    if not connection(workspace):
        raise HTTPException(409, "Connect Google Calendar before issuing appointment links.")
    link_id = rpc("issue_appointment_booking_link", {
        "p_workspace": workspace, "p_service": body.service, "p_duration": body.duration_minutes,
    })
    if not link_id:
        raise HTTPException(429, "A workspace can have five active links and issue up to ten per 24 hours.")
    response.headers.update(PRIVATE_HEADERS)
    return {"url": link_url(UUID(link_id))}


@router.delete(PATH + "/{link_id}")
def revoke_booking_link(oid: UUID, link_id: UUID, user=Depends(current_user)):
    workspace = str(oid)
    membership(workspace, user, MANAGERS)
    rows = api(TABLE, "PATCH", params={"organization_id": "eq." + workspace, "id": "eq." + str(link_id),
        "status": "eq.open", "select": "id"}, data={"status": "revoked"}, prefer="return=representation")
    if not rows:
        raise HTTPException(409, "Only an unused link can be revoked.")
    return {"revoked": True}


@router.get("/book", response_class=HTMLResponse, include_in_schema=False)
def booking_page():
    return HTMLResponse(TEMPLATE.read_text(), headers=PRIVATE_HEADERS)


@router.get("/api/public/booking-link", include_in_schema=False)
def booking_link_info(response: Response, link=Depends(authorized_link)):
    workspace = org_or_404(link["organization_id"])
    response.headers.update(PRIVATE_HEADERS)
    output = {"business": workspace["name"], "timezone": workspace["timezone"], "service": link["service"],
              "duration_minutes": link["duration_minutes"], "status": link["status"]}
    if link["status"] == "booked" and link["appointment_id"]:
        rows = api("appointment_requests", params={"organization_id": "eq." + link["organization_id"],
            "id": "eq." + link["appointment_id"], "select": "requested_time,status,duration_minutes", "limit": 1})
        if rows:
            output["start"] = rows[0]["requested_time"]
            output["appointment_status"] = rows[0]["status"]
            output["duration_minutes"] = rows[0]["duration_minutes"]
    return output


@router.post("/api/public/booking-link/check", include_in_schema=False)
def check_booking_link(body: RequestedTime, response: Response, link=Depends(authorized_link)):
    if link["status"] != "open":
        raise HTTPException(409, "This link is already processing an appointment.")
    start, end = check_slot(Slot(start=body.start, duration_minutes=link["duration_minutes"]))
    workspace = link["organization_id"]
    if link["checks"] >= 24:
        raise HTTPException(429, "This link has reached its availability limit. Contact the business for help.")
    updated = api(TABLE, "PATCH", params={"id": "eq." + link["id"], "organization_id": "eq." + workspace,
        "status": "eq.open", "checks": "eq." + str(link["checks"]), "select": "id"},
        data={"checks": link["checks"] + 1}, prefer="return=representation")
    if not updated:
        raise HTTPException(409, "The link changed. Retry the availability check.")
    response.headers.update(PRIVATE_HEADERS)
    if not within_booking_hours(workspace, start, end):
        return {"available": False, "message": OUTSIDE_HOURS}
    available = free_busy(google_token(workspace), start, end)
    return {"available": available, "message": "This time is available." if available else "Choose another time."}


@router.post("/api/public/booking-link/book", include_in_schema=False)
def book_with_link(body: Booking, response: Response, link=Depends(authorized_link)):
    response.headers.update(PRIVATE_HEADERS)
    if link["status"] == "booked":
        return {"confirmed": True}
    if not body.confirm:
        raise HTTPException(422, "Confirm that you want to book this appointment.")
    valid_contact = re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", body.contact) or (
        re.fullmatch(r"[+() .0-9-]+", body.contact) and 7 <= len(re.sub(r"\D", "", body.contact)) <= 15)
    if not valid_contact:
        raise HTTPException(422, "Enter a valid email address or callback number.")
    duration = link["duration_minutes"]
    start, end = check_slot(Slot(start=body.start, duration_minutes=duration))
    workspace = link["organization_id"]
    payload = json.dumps([start.astimezone(timezone.utc).isoformat(), body.name, body.contact], ensure_ascii=False)
    proof = hmac.digest(signature(UUID(link["id"]), PURPOSE).encode(), payload.encode(), "sha256").hex()
    request_id = rpc("claim_appointment_booking_link", {
        "p_link": link["id"], "p_workspace": workspace, "p_hash": proof,
    })
    if not request_id:
        raise HTTPException(409, "An appointment may already be processing. Wait briefly and retry with the original details.")
    key = {"organization_id": "eq." + workspace, "id": "eq." + link["id"],
           "status": "eq.pending", "request_id": "eq." + request_id}
    try:
        result = confirm_calendar_booking(workspace, "booking-link-" + request_id,
            start, end, duration, body.name, body.contact, link["service"])
    except (ValueError, HTTPException) as exc:
        api(TABLE, "PATCH", params=key, data={"lease_until": None}, prefer="return=minimal")
        raise HTTPException(502, "The calendar could not confirm this appointment. Retry with the same details.") from exc
    if not result["confirmed"]:
        api(TABLE, "PATCH", params=key,
            data={"status": "open", "request_hash": None, "request_id": None, "lease_until": None}, prefer="return=minimal")
        raise HTTPException(409, "This time is no longer available. Choose another time.")
    api(TABLE, "PATCH", params=key, data={"status": "booked", "appointment_id": result["appointment_id"],
        "request_hash": None, "request_id": None, "lease_until": None}, prefer="return=minimal")
    return {"confirmed": True, "start": start.isoformat()}
