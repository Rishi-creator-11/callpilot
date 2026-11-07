"""Deterministic call admission rules and scoped Vapi specialist assistants."""

import hmac
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from app.account import current_user, membership, rpc
from app.booking_hours import within_booking_hours
from app.cloud import api, org_or_404
from app.common import MAX_CALL_SECONDS, public_base
from app.identity import SPEECH_FIELDS, speech_policy, transcriber, voice_config
from app.voice_preview import answer_preview_tools

router = APIRouter(tags=["inbound-routing"])
PATH = "/api/account/organizations/{oid}/inbound-routing"
DEFAULT = {"enabled": False, "runtime_workers": False, "rules": []}


class RouteRule(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    label: str = Field(min_length=2, max_length=60)
    caller_prefix: str = Field(default="", pattern=r"^(?:\+[1-9][0-9]{0,14})?$")
    anonymous: bool = False
    outside_hours: bool = False
    action: Literal["operator", "reject", "fallback", "worker"]
    worker_name: str = Field(default="", max_length=80)


class RoutingSettings(BaseModel):
    enabled: bool = False
    runtime_workers: bool = False
    rules: list[RouteRule] = Field(default_factory=list, max_length=12)


class RoutingEdit(BaseModel):
    previous: RoutingSettings
    settings: RoutingSettings


def routing_config(workspace: str) -> dict:
    rows = api("agent_configs", params={
        "organization_id": "eq." + workspace,
        "select": "inbound_routing,worker_playbooks,agent_name,greeting," + SPEECH_FIELDS,
        "limit": 1,
    })
    return rows[0] if rows else {}


def enabled_playbooks(config: dict) -> list:
    return [entry for entry in (config.get("worker_playbooks") or []) if entry.get("enabled")]


@router.get(PATH)
def get_routing(oid: UUID, user=Depends(current_user)):
    workspace = str(oid)
    role = membership(workspace, user, ("owner", "admin"))
    cfg = routing_config(workspace)
    billing = api("billing_accounts", params={"organization_id": "eq." + workspace,
        "select": "enforcement_enabled", "limit": 1})
    routes = api("runtime_call_routes", params={"organization_id": "eq." + workspace,
        "select": "route,matched_rule,created_at", "order": "created_at.desc", "limit": 10})
    return {"settings": cfg.get("inbound_routing") or DEFAULT, "recent_routes": routes, "can_manage": role == "owner",
            "workers": [item["name"] for item in enabled_playbooks(cfg)],
            "admission_enabled": bool(billing and billing[0].get("enforcement_enabled"))}


@router.put(PATH)
def save_routing(oid: UUID, body: RoutingEdit, user=Depends(current_user)):
    workspace = str(oid)
    membership(workspace, user, ("owner",))
    settings = body.settings
    names = {worker["name"] for worker in enabled_playbooks(routing_config(workspace))}
    if settings.runtime_workers and not names:
        raise HTTPException(422, "Enable at least one worker playbook before using runtime workers.")
    if len({rule.label.casefold() for rule in settings.rules}) != len(settings.rules):
        raise HTTPException(422, "Give each rule a different name.")
    for rule in settings.rules:
        if not (rule.caller_prefix or rule.anonymous or rule.outside_hours):
            raise HTTPException(422, "Choose at least one condition for each rule.")
        if rule.anonymous and rule.caller_prefix:
            raise HTTPException(422, "An anonymous caller cannot also match a phone prefix.")
        if rule.action == "worker" and (not settings.runtime_workers or rule.worker_name not in names):
            raise HTTPException(422, "Select an enabled runtime worker for each worker route.")
    result = body.settings.model_dump()
    if rpc("save_inbound_routing", {
        "p_workspace": workspace, "p_previous": body.previous.model_dump(), "p_routing": result,
    }) is not True:
        raise HTTPException(409, "The saved rules changed. Refresh and review the latest version.")
    return {"settings": result}


def choose_route(workspace: str, settings: dict, number: str, now: datetime) -> dict:
    outside = None
    for rule in settings.get("rules", []):
        if rule["caller_prefix"] and not number.startswith(rule["caller_prefix"]):
            continue
        if rule["anonymous"] and number:
            continue
        if rule["outside_hours"]:
            if outside is None:
                outside = not within_booking_hours(workspace, now, now + timedelta(seconds=1))
            if not outside:
                continue
        return rule
    return {"action": "operator", "worker_name": ""}


def worker_token(workspace: str) -> str:
    return hmac.digest(os.environ["VAPI_WEBHOOK_TOKEN"].encode(), ("callpilot-runtime:" + workspace).encode(), "sha256").hex()


def squad_for(workspace: str, config: dict, operator_id: str, first_worker: str) -> dict | None:
    workers = enabled_playbooks(config)
    if not workers:
        return None
    secret = os.getenv("VAPI_WEBHOOK_TOKEN")
    if not secret:
        raise HTTPException(503, "Runtime worker credentials are unavailable.")
    business = org_or_404(workspace)["name"]
    base = public_base()
    operator_handoffs = []
    specialists = []
    for index, worker in enumerate(workers, 1):
        name = f"CallPilotSpecialist{index}"
        first = worker["name"] == first_worker
        operator_handoffs.append({"type": "assistant", "assistantName": name,
                                  "description": worker["name"] + ": " + worker["when"],
                                  "contextEngineeringPlan": {"type": "all"}})
        knowledge = {
            "type": "function", "async": False,
            "function": {"name": "lookup_approved_knowledge", "description": "Get approved answers for factual business questions.",
                         "parameters": {"type": "object", "properties": {"question": {"type": "string"}},
                                        "required": ["question"], "additionalProperties": False}},
            "server": {"url": f"{base}/webhooks/runtime-worker/{workspace}",
                       "headers": {"Authorization": "Bearer " + worker_token(workspace)}, "timeoutSeconds": 12},
        }
        handoff = {"type": "handoff", "function": {"name": "return_to_operator"},
                   "destinations": [{"type": "assistant", "assistantId": operator_id,
                                     "description": "Return to the main receptionist for any action or other topic.",
                                     "contextEngineeringPlan": {"type": "all"}}]}
        guidance = (
            f"You are the AI receptionist for {business}. Your specialist role is {worker['name']}. "
            "Answer factual questions only from lookup_approved_knowledge. Do not make up policies or confirm any action. "
            "For bookings, transfers, order requests, callback capture, or a topic outside your role, "
            "handoff back to the operator without announcing an internal transfer. "
            "If the caller entered mid-conversation, do not greet again. " + speech_policy(config)
            + " Workspace guidance, subject to these limits: " + worker["instructions"]
        )
        assistant = {
            "name": name, "firstMessageMode": "assistant-speaks-first" if first else "assistant-waits-for-user",
            "model": {"provider": "openai", "model": "gpt-4o-mini", "temperature": 0,
                      "messages": [{"role": "system", "content": guidance}], "tools": [knowledge, handoff, {"type": "endCall"}]},
            "voice": voice_config(config, {"provider": "vapi", "voiceId": "Elliot", "version": 2}),
            "transcriber": transcriber(config), "maxDurationSeconds": MAX_CALL_SECONDS,
            "server": {"url": f"{base}/webhooks/vapi/{workspace}", "headers": {"Authorization": "Bearer " + secret}},
            "serverMessages": ["status-update", "end-of-call-report"],
            "artifactPlan": {"recordingEnabled": False, "transcriptPlan": {"enabled": False}},
        }
        if first:
            assistant["firstMessage"] = config["greeting"]
        specialists.append((first, {"assistant": assistant}))
    operator = {"assistantId": operator_id, "assistantOverrides": {"model": {"tools:append": [
        {"type": "handoff", "function": {"name": "handoff_to_specialist"}, "destinations": operator_handoffs}
    ]}}}
    return {"members": [member for first, member in specialists if first] + [operator]
                       + [member for first, member in specialists if not first]}


def runtime_admission(workspace: str, phone_id: str, agent_id: str, message: dict, fallback: dict) -> dict:
    config = routing_config(workspace)
    settings = config.get("inbound_routing") or DEFAULT
    if not settings["enabled"]:
        return {"assistantId": agent_id}
    call = message.get("call") or {}
    customer = message.get("customer") or call.get("customer") or {}
    candidate = customer.get("number", "") if isinstance(customer, dict) else ""
    number = candidate if isinstance(candidate, str) and re.fullmatch(r"\+[1-9][0-9]{7,14}", candidate) else ""
    route = choose_route(workspace, settings, number, datetime.now(timezone.utc))
    action = route["action"]
    response = {"assistantId": agent_id}
    if action == "reject":
        response = {"error": "This line is not available for this call. Please contact the business directly."}
    elif action == "fallback":
        if fallback.get("fallback_enabled") and fallback.get("fallback_number"):
            response = {"destination": {"type": "number", "number": fallback["fallback_number"],
                                        "message": "Connecting you to the business team."}}
        else:
            response = {"error": "A transfer destination is unavailable. Please contact the business directly."}
    elif settings["runtime_workers"]:
        if action == "worker" and route["worker_name"] not in {worker["name"] for worker in enabled_playbooks(config)}:
            action = "operator"
        squad = squad_for(workspace, config, agent_id, route["worker_name"] if action == "worker" else "")
        if squad:
            response = {"squad": squad}
            action = "runtime_worker" if action == "worker" else "runtime_operator"
    call_id = call.get("id")
    if isinstance(call_id, str) and 4 <= len(call_id) <= 150:
        api("runtime_call_routes", "POST", params={"on_conflict": "organization_id,provider_call_id"},
            data={"organization_id": workspace, "provider_call_id": call_id, "provider_phone_id": phone_id,
                  "route": action, "matched_rule": route.get("label")},
            prefer="resolution=ignore-duplicates,return=minimal")
    elif "squad" in response:
        return {"error": "The line could not securely identify this call."}
    return response


def is_routed_worker_call(workspace: str, call: dict) -> bool:
    call_id, phone_id = call.get("id"), call.get("phoneNumberId")
    if not isinstance(call_id, str) or not isinstance(phone_id, str):
        return False
    return bool(api("runtime_call_routes", params={
        "organization_id": "eq." + workspace, "provider_call_id": "eq." + call_id,
        "provider_phone_id": "eq." + phone_id, "route": "in.(runtime_worker,runtime_operator)",
        "select": "provider_call_id", "limit": 1,
    }))


@router.post("/webhooks/runtime-worker/{oid}", include_in_schema=False)
async def worker_knowledge(oid: UUID, request: Request, authorization: str | None = Header(default=None)):
    workspace = str(oid)
    if not os.getenv("VAPI_WEBHOOK_TOKEN") or not hmac.compare_digest(
        (authorization or "").encode(), ("Bearer " + worker_token(workspace)).encode()
    ):
        raise HTTPException(401, "Invalid worker credential.")
    return await answer_preview_tools(workspace, request)
