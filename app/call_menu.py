"""Workspace-owned, exact-choice branching for the managed operator."""

import json
import unicodedata
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.account import current_user, membership
from app.cloud import api

router = APIRouter(prefix="/api/account/organizations/{oid}/workers/call-menu")
MANAGERS = ("owner", "admin")
NEXT_STEPS = {
    "operator": "Continue with the regular receptionist and approved knowledge.",
    "appointment": "Ask what service and time the caller wants. Follow the normal availability and booking rules.",
    "order": "Ask which product the caller needs. Follow the normal catalog and pending order request rules.",
    "callback": "Ask if the caller wants the team to call back. Follow the normal lead capture and consent rules.",
    "transfer": "Ask which enabled department the caller wants. Follow the normal transfer and consent rules.",
}
REQUIREMENT = {
    "appointment": "voice_booking_enabled",
    "order": "voice_order_requests_enabled",
    "callback": "voice_leads_enabled",
}


class Option(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    phrase: str = Field(min_length=1, max_length=48)
    action: Literal["operator", "appointment", "order", "callback", "transfer"]


class Menu(BaseModel):
    enabled: bool = False
    options: list[Option] = Field(max_length=8)


class Update(BaseModel):
    revision: UUID
    menu: Menu


def normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split()).strip(" .,!?:;¿¡")


def can_route(action: str, flags: dict) -> bool:
    if action == "transfer":
        return any(item.get("enabled") for item in flags.get("transfer_directory") or [])
    return action not in REQUIREMENT or bool(flags.get(REQUIREMENT[action]))


@router.get("")
def get_menu(oid: UUID, user=Depends(current_user)):
    role = membership(str(oid), user)
    rows = api("agent_configs", params={"organization_id": "eq." + str(oid),
        "select": "call_menu,call_menu_revision", "limit": 1})
    if not rows:
        raise HTTPException(404, "This workspace has no agent configuration.")
    return {"menu": rows[0]["call_menu"], "revision": rows[0]["call_menu_revision"],
            "can_edit": role in MANAGERS}


@router.put("")
def save_menu(oid: UUID, body: Update, user=Depends(current_user)):
    membership(str(oid), user, MANAGERS)
    from app.voice_actions import feature_flags

    phrases = [normalize(option.phrase) for option in body.menu.options]
    if any(not phrase or phrase.isdecimal() for phrase in phrases) or len(phrases) != len(set(phrases)):
        raise HTTPException(422, "Give each branch a distinct spoken phrase. Numbers are assigned automatically.")
    if body.menu.enabled:
        if not phrases:
            raise HTTPException(422, "Add at least one option before enabling the menu.")
        flags = feature_flags(str(oid))
        if any(not can_route(option.action, flags) for option in body.menu.options):
            raise HTTPException(422, "Enable the actions for these branches in Launch Studio or Channels first.")
    revision = str(uuid4())
    menu = body.menu.model_dump()
    rows = api("agent_configs", "PATCH", params={"organization_id": "eq." + str(oid),
        "call_menu_revision": "eq." + str(body.revision)}, data={"call_menu": menu,
        "call_menu_revision": revision}, prefer="return=representation")
    if not rows:
        raise HTTPException(409, "The menu changed. Refresh the saved menu before editing again.")
    return {"menu": menu, "revision": revision}


def resolve(flags: dict, spoken: str) -> str:
    menu = flags.get("call_menu") or {}
    if not menu.get("enabled"):
        return "No active call menu. Continue as the regular receptionist."
    options = [option for option in menu.get("options", []) if can_route(option["action"], flags)]
    if not options:
        return "No available menu choices. Continue as the regular receptionist."
    if not spoken:
        return "Available spoken choices (quote these labels only): " + json.dumps(
            [option["phrase"] for option in options], ensure_ascii=False)
    for index, option in enumerate(options, 1):
        if normalize(spoken) in (normalize(option["phrase"]), str(index)):
            return "Matched branch: " + NEXT_STEPS[option["action"]]
    return "No matching choice. Ask for one of the menu choices or continue as the regular receptionist."
