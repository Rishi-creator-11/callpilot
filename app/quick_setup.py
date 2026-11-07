"""Quick setup: turn a few ticked goals into a ready-to-test receptionist.

Owners do not write prompts. The server builds the greeting and instructions from the business name,
the chosen goals and an optional one-line note, switches on the matching abilities that are safe to
enable, creates the managed assistant when missing, syncs it, and reports what still needs a connection.
"""
from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.account import AgentEdit, current_user, membership, save_agent_configuration
from app.cloud import api, org_or_404

router = APIRouter(prefix="/api/account/organizations/{oid}/quick-setup", tags=["quick-setup"])
log = logging.getLogger("callpilot")

GOALS = {
    "info": "Answer questions",
    "messages": "Take messages and capture leads",
    "quotes": "Collect details for quotes",
    "orders": "Take orders",
    "booking": "Book appointments",
    "transfer": "Connect callers to a person",
}

SECTIONS = {
    "info": "Answer questions about the business using only the approved information you can look up. "
            "If you do not have the answer, say so and offer to take a message.",
    "messages": "Take messages and capture leads: ask for the caller's name and best callback number, "
                "repeat the number back, and save the request only after the caller agrees to be contacted.",
    "quotes": "When a caller wants a quote, collect what they need, the quantity or size, their deadline and "
              "their contact details, then save it for the team. Do not state a price unless the business has "
              "approved exact prices. Tell the caller a team member will follow up with the quote.",
    "orders": "Take order requests: confirm the exact product and quantity, read the details back, and submit the "
              "request only after a clear yes. Never say an order is placed or paid. Staff review every request.",
    "booking": "Book appointments: check availability first, collect name, contact and purpose, read the details "
               "back, and book only after the caller clearly agrees.",
    "transfer": "If the caller asks for a person, or the issue is urgent, offer to connect them to the team.",
}


class QuickSetup(BaseModel):
    goals: list[str] = Field(min_length=1, max_length=len(GOALS))
    other: str | None = Field(default=None, max_length=200)
    transfer_label: str | None = Field(default=None, max_length=60)
    transfer_number: str | None = Field(default=None, pattern=r"^\+[1-9][0-9]{7,14}$")

    @field_validator("goals")
    @classmethod
    def known(cls, goals):
        unique = list(dict.fromkeys(goals))
        if any(goal not in GOALS for goal in unique):
            raise ValueError("Choose from the listed goals.")
        return unique

    @field_validator("other")
    @classmethod
    def clean(cls, value):
        return " ".join((value or "").split()) or None


def compose(business: str, industry: str | None, name: str, goals: list[str], other: str | None) -> tuple[str, str]:
    shop = " ".join(business.split())[:80]
    greeting = "Thanks for calling %s. I'm %s, the AI receptionist. How can I help you today?" % (shop, name)
    field = (" in the %s space" % industry.strip()[:40]) if industry and industry.strip().lower() != "general" else ""
    parts = ["You are %s, the AI phone receptionist for %s%s. Speak warmly, in short sentences, and keep every "
             "reply brief. Never invent prices, hours, policies or promises." % (name, shop, field)]
    parts += [SECTIONS[goal] for goal in GOALS if goal in goals]
    if other:
        parts.append("Additional guidance from the business owner: " + other)
    return greeting, "\n\n".join(parts)[:2400]


def prerequisites(workspace: str, goals: list[str]) -> dict:
    from app.calendar import connection
    flags = {"voice_leads_enabled": any(g in goals for g in ("messages", "quotes", "orders")),
             "voice_booking_enabled": False, "voice_catalog_enabled": False, "voice_order_requests_enabled": False}
    needs = []
    if "booking" in goals:
        if connection(workspace):
            flags["voice_booking_enabled"] = True
        else:
            needs.append({"key": "calendar", "text": "Connect Google Calendar so the agent can book appointments.", "page": "appointments"})
    if "orders" in goals:
        approved = api("catalog_items", params={"organization_id": "eq." + workspace, "active": "eq.true",
                                                 "approved_at": "not.is.null", "select": "id", "limit": 1})
        if approved:
            flags["voice_catalog_enabled"] = flags["voice_order_requests_enabled"] = True
        else:
            needs.append({"key": "products", "text": "Add and approve at least one product so the agent can take orders.", "page": "studio"})
    return {"flags": flags, "needs": needs}


@router.get("")
def status(oid: UUID, user=Depends(current_user)):
    workspace = str(oid)
    membership(workspace, user)
    rows = api("agent_configs", params={"organization_id": "eq." + workspace, "limit": 1,
        "select": "agent_name,greeting,provider_agent_id,voice_leads_enabled,voice_booking_enabled,"
                  "voice_catalog_enabled,voice_order_requests_enabled,transfer_directory"})
    docs = api("knowledge_documents", params={"organization_id": "eq." + workspace, "select": "id,approved_at", "limit": 50})
    config = rows[0] if rows else {}
    return {"goals": list(GOALS.items()), "agent_name": config.get("agent_name"),
            "assistant_connected": bool(config.get("provider_agent_id")),
            "knowledge_sources": len(docs), "knowledge_approved": sum(1 for d in docs if d.get("approved_at")),
            "enabled": {"messages": bool(config.get("voice_leads_enabled")), "booking": bool(config.get("voice_booking_enabled")),
                        "orders": bool(config.get("voice_order_requests_enabled")),
                        "transfer": bool(config.get("transfer_directory"))}}


@router.post("")
def build(oid: UUID, body: QuickSetup, user=Depends(current_user)):
    workspace = str(oid)
    membership(workspace, user, ("owner", "admin"))
    business = org_or_404(workspace)
    rows = api("agent_configs", params={"organization_id": "eq." + workspace, "limit": 1,
        "select": "agent_name,provider_agent_id,transfer_directory"})
    if not rows:
        raise HTTPException(404, "No receptionist is configured for this workspace")
    config = rows[0]
    name = (config.get("agent_name") or "Alex").strip() or "Alex"
    greeting, instructions = compose(business["name"], business.get("industry"), name, body.goals, body.other)
    save_agent_configuration(workspace, AgentEdit(agent_name=name, greeting=greeting, call_instructions=instructions))

    plan = prerequisites(workspace, body.goals)
    needs = list(plan["needs"])
    updates = dict(plan["flags"])
    if "transfer" in body.goals:
        if body.transfer_number and not config.get("transfer_directory"):
            # Same validation and atomic save as the Channels transfer directory (premium-rate and loop checks).
            from app.transfer_directory import Destination, persist_directory, validate_destinations
            destination = Destination(label=(body.transfer_label or "Front desk").strip() or "Front desk",
                                      number=body.transfer_number,
                                      when="The caller asks for a person or the issue is urgent.")
            persist_directory(workspace, [], validate_destinations(workspace, [destination]))
        elif not body.transfer_number and not config.get("transfer_directory"):
            needs.append({"key": "transfer", "text": "Add a phone number to connect callers to a person.", "page": "channels"})
    api("agent_configs", "PATCH", params={"organization_id": "eq." + workspace}, data=updates, prefer="return=minimal")

    assistant, synced = bool(config.get("provider_agent_id")), False
    if not assistant:
        try:
            from app.cloud import provision_test_agent
            assistant = bool(provision_test_agent(workspace).get("assistant_id"))
        except HTTPException as exc:
            log.warning("quick_setup_provision_failed workspace=%s status=%s", workspace, exc.status_code)
    if assistant:
        try:
            from app.call_intel import refresh_receptionist_prompt
            result = refresh_receptionist_prompt(workspace, use_saved_greeting=True)
            synced = result.get("reason") in ("already_current", "voice_policy_and_one_second_opening_synced")
        except HTTPException as exc:
            log.warning("quick_setup_sync_failed workspace=%s status=%s", workspace, exc.status_code)
    if not assistant:
        needs.append({"key": "assistant", "text": "We could not create the phone assistant yet. Try again, or contact support.", "page": "studio"})
    elif not synced:
        needs.append({"key": "sync", "text": "Your settings are saved, but they could not be applied to the live assistant yet.", "page": "builder"})
    enabled = [GOALS[g] for g in GOALS if g in body.goals and not any(n["key"] == {"booking": "calendar", "orders": "products"}.get(g) for n in needs)]
    return {"agent_name": name, "greeting": greeting, "instructions": instructions, "enabled": enabled,
            "needs": needs, "assistant_ready": assistant, "synced": synced}
