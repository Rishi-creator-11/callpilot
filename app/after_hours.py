"""Closed-hours greeting: chosen server-side at call admission from the workspace's open hours."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator

from app.account import current_user, membership
from app.booking_hours import get_settings, slot_allowed
from app.cloud import api
from app.common import opening_hook, wrapup_hook

router = APIRouter(prefix="/api/account/organizations/{oid}/after-hours", tags=["after-hours"])
log = logging.getLogger("callpilot")


class ClosedGreeting(BaseModel):
    closed_greeting: str | None = Field(default=None, max_length=500)

    @field_validator("closed_greeting")
    @classmethod
    def clean(cls, value):
        value = (value or "").strip()
        if not value:
            return None
        if len(value) < 10:
            raise ValueError("Use at least 10 characters for the closed-hours greeting.")
        return value


def is_open_now(workspace: str, now: datetime | None = None) -> bool:
    """True when no open hours are configured, or the instant falls inside a window."""
    settings = get_settings(workspace)
    if settings.hours is None:
        return True
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return slot_allowed(settings, moment, moment + timedelta(seconds=1))


def closed_greeting(workspace: str) -> str | None:
    rows = api("agent_configs", params={"organization_id": "eq." + workspace, "select": "closed_greeting", "limit": 1})
    return (rows[0].get("closed_greeting") or None) if rows else None


def apply_after_hours(workspace: str, agent_id: str, result: dict) -> dict:
    """Add a closed greeting to an admission result for the operator assistant. Never blocks a call."""
    try:
        if result.get("assistantId") != agent_id or "assistantOverrides" in result:
            return result
        greeting = closed_greeting(workspace)
        if not greeting or is_open_now(workspace):
            return result
        return {**result, "assistantOverrides": {
            "firstMessage": greeting, "hooks": [opening_hook(greeting), wrapup_hook()]}}
    except Exception as exc:  # noqa: BLE001 - an hours or database error must not stop the call
        log.warning("after_hours_failed workspace=%s error=%s", workspace, type(exc).__name__)
        return result


@router.get("")
def get_after_hours(oid: UUID, user=Depends(current_user)):
    workspace = str(oid)
    membership(workspace, user)
    settings = get_settings(workspace)
    return {"closed_greeting": closed_greeting(workspace), "hours_configured": settings.hours is not None,
            "timezone": settings.timezone, "open_now": is_open_now(workspace)}


@router.put("")
def save_after_hours(oid: UUID, body: ClosedGreeting, user=Depends(current_user)):
    workspace = str(oid)
    membership(workspace, user, ("owner", "admin"))
    api("agent_configs", "PATCH", params={"organization_id": "eq." + workspace},
        data={"closed_greeting": body.closed_greeting}, prefer="return=minimal")
    return {"closed_greeting": body.closed_greeting}
