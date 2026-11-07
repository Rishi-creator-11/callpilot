"""Public enterprise inquiry form: validated, rate limited and stored for the CallPilot team."""
from __future__ import annotations

import re
from collections import deque
from threading import Lock
from time import monotonic

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from app.cloud import api

router = APIRouter(tags=["enterprise"])
_hits: deque = deque()
_lock = Lock()
PER_IP_PER_HOUR = 3
GLOBAL_PER_HOUR = 60


class Inquiry(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    email: str = Field(min_length=5, max_length=200)
    company: str = Field(min_length=2, max_length=120)
    phone: str | None = Field(default=None, max_length=40)
    monthly_calls: str | None = Field(default=None, max_length=40)
    message: str | None = Field(default=None, max_length=2000)
    website: str = Field(default="", max_length=200)  # honeypot: real visitors leave it empty

    @field_validator("email")
    @classmethod
    def valid_email(cls, value):
        value = value.strip()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("Enter a valid email address.")
        return value

    @field_validator("name", "company")
    @classmethod
    def trimmed(cls, value):
        value = value.strip()
        if len(value) < 2:
            raise ValueError("This field is too short.")
        return value


def allow(client: str) -> bool:
    now = monotonic()
    with _lock:
        while _hits and _hits[0][0] <= now - 3600:
            _hits.popleft()
        if len(_hits) >= GLOBAL_PER_HOUR or sum(1 for _, who in _hits if who == client) >= PER_IP_PER_HOUR:
            return False
        _hits.append((now, client))
        return True


@router.post("/api/enterprise-inquiry", status_code=201)
def submit(body: Inquiry, request: Request):
    if body.website:  # Bots fill hidden fields. Pretend success and store nothing.
        return {"received": True}
    # Only the rightmost entry was added by our own proxy; earlier entries can be forged by the sender.
    forwarded = [part.strip() for part in request.headers.get("x-forwarded-for", "").split(",") if part.strip()]
    client = forwarded[-1] if forwarded else (request.client.host if request.client else "unknown")
    if not allow(client):
        raise HTTPException(429, "Too many requests. Please try again later.")
    api("enterprise_inquiries", "POST", data={
        "name": body.name, "email": body.email, "company": body.company,
        "phone": (body.phone or "").strip() or None, "monthly_calls": (body.monthly_calls or "").strip() or None,
        "message": (body.message or "").strip() or None,
    }, prefer="return=minimal")
    return {"received": True}
