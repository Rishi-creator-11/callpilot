"""Structured intake and session storage for CallPilot runtime specialists."""

import hmac
import json
import os
import re
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from app.account import current_user, membership
from app.cloud import api
from app.common import public_base

router = APIRouter(tags=["worker-forms"])
PATH = "/api/account/organizations/{oid}/workers/forms"
MANAGERS = ("owner", "admin")
FORMS = "runtime_worker_forms"
SUBMISSIONS = "runtime_worker_submissions"
COLUMNS = "id,worker_name,revision,configuration"


class IntakeField(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    kind: Literal["name", "phone", "email", "postal", "text"]
    question: str = Field(min_length=3, max_length=160)
    required: bool = True


class FormSettings(BaseModel):
    fields: list[IntakeField] = Field(max_length=10)
    after: Literal["operator", "end"] = "operator"


class SaveForm(BaseModel):
    worker_name: str = Field(min_length=2, max_length=80)
    revision: UUID | None = None
    configuration: FormSettings


def runtime_forms(workspace: str) -> list:
    return api(FORMS, params={"organization_id": "eq." + workspace, "select": COLUMNS, "limit": 200})


@router.get(PATH)
def list_forms(oid: UUID, response: Response, user=Depends(current_user)):
    workspace = str(oid)
    membership(workspace, user, MANAGERS)
    response.headers["Cache-Control"] = "no-store"
    return {"forms": runtime_forms(workspace)}


@router.put(PATH)
def save_form(oid: UUID, body: SaveForm, user=Depends(current_user)):
    workspace = str(oid)
    membership(workspace, user, MANAGERS)
    agents = api("agent_configs", params={"organization_id": "eq." + workspace,
        "select": "worker_playbooks", "limit": 1})
    if not agents or not any(entry.get("name") == body.worker_name for entry in agents[0]["worker_playbooks"]):
        raise HTTPException(409, "Save this worker before configuring its intake form.")
    settings = body.configuration.model_dump()
    if settings["fields"] and not any(field["required"] for field in settings["fields"]):
        raise HTTPException(422, "Make at least one question required.")
    revision = str(uuid4())
    if body.revision:
        rows = api(FORMS, "PATCH", params={"organization_id": "eq." + workspace,
            "worker_name": "eq." + body.worker_name, "revision": "eq." + str(body.revision), "select": COLUMNS},
            data={"configuration": settings, "revision": revision}, prefer="return=representation")
    else:
        rows = api(FORMS, "POST", params={"on_conflict": "organization_id,worker_name", "select": COLUMNS},
            data={"organization_id": workspace, "worker_name": body.worker_name, "configuration": settings,
                  "revision": revision}, prefer="resolution=ignore-duplicates,return=representation")
    if not rows:
        raise HTTPException(409, "This worker form changed. Reload it before saving.")
    return rows[0]


@router.get(PATH + "/{form_id}/submissions")
def recent_submissions(oid: UUID, form_id: UUID, response: Response, user=Depends(current_user)):
    membership(str(oid), user, MANAGERS)
    response.headers["Cache-Control"] = "no-store"
    return api(SUBMISSIONS, params={"organization_id": "eq." + str(oid), "form_id": "eq." + str(form_id),
        "select": "created_at,answers", "order": "created_at.desc", "limit": 20})


def form_token(workspace: str, form_id: str, revision: str) -> str:
    secret = os.environ["VAPI_WEBHOOK_TOKEN"]
    return hmac.digest(secret.encode(), f"callpilot-worker-form:{workspace}:{form_id}:{revision}".encode(),
                       "sha256").hex()


def specialist_form_tool(workspace: str, form: dict) -> tuple[dict, str]:
    settings = form["configuration"]
    fields = {"field_" + str(index): {"type": "string", "description":
        field["kind"] + " · " + field["question"] + (" (required)" if field["required"] else " (optional)")}
        for index, field in enumerate(settings["fields"], 1)}
    after = (
        "After it saves, thank the caller, then use endCall when they are ready."
        if settings["after"] == "end" else
        "After it saves, acknowledge receipt and use return_to_operator if the caller needs anything else."
    )
    tool = {"type": "function", "async": False,
        "function": {"name": "save_worker_intake",
            "description": "Save the specialist's intake to this call after the caller agrees to share their answers.",
            "parameters": {"type": "object", "properties": {**fields,
                "consent": {"type": "boolean", "description": "True only after the caller agrees to save their answers."}},
                "required": ["consent", *fields.keys()], "additionalProperties": False}},
        "server": {"url": f"{public_base()}/webhooks/worker-form/{workspace}/{form['id']}/{form['revision']}",
            "headers": {"Authorization": "Bearer " + form_token(workspace, form["id"], form["revision"])},
            "timeoutSeconds": 12}}
    guidance = (
        "For callers seeking this specialist's help, collect the save_worker_intake fields in order, one question "
        "at a time. Use the owner's questions as prompts. Use answers the caller already gave if they confirm them. "
        "An empty string is allowed only for optional answers. Never guess a required answer. "
        "Ask whether the caller agrees to save their answers, then call save_worker_intake. "
        "If they decline, continue or return to the operator without saving. Do not claim it saved until the tool succeeds. "
        + after
    )
    return tool, guidance


def validate_answer(field: dict, value: object) -> str:
    answer = " ".join(value.split()) if isinstance(value, str) else ""
    if not answer:
        if field["required"]:
            raise ValueError("Ask the caller for this required answer: " + field["question"])
        return ""
    kind = field["kind"]
    if len(answer) > (500 if kind == "text" else 180):
        raise ValueError("Ask for a shorter answer: " + field["question"])
    if kind == "phone" and (not re.fullmatch(r"\+?[0-9 ()-]{7,28}", answer)
            or not 7 <= len(re.sub(r"\D", "", answer)) <= 15):
        raise ValueError("Ask for a valid callback phone number.")
    if kind == "email" and not re.fullmatch(r"[^@\s]+@[^@\s]+[.][^@\s]+", answer):
        raise ValueError("Ask for a valid email address.")
    if kind == "postal" and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 -]{1,15}", answer):
        raise ValueError("Ask for a valid postal code.")
    return answer


def save_intake(workspace: str, form_id: str, revision: str, call: dict, arguments: dict) -> str:
    from app.runtime_routing import is_routed_worker_call

    if not is_routed_worker_call(workspace, call):
        raise ValueError("This call is not attached to the workspace's live specialist.")
    if arguments.get("consent") is not True:
        raise ValueError("Ask the caller before saving their answers.")
    forms = api(FORMS, params={"organization_id": "eq." + workspace, "id": "eq." + form_id,
        "revision": "eq." + revision, "select": COLUMNS, "limit": 1})
    if not forms:
        raise ValueError("The intake form changed. Return to the operator for help.")
    form = forms[0]
    fields = form["configuration"]["fields"]
    if not fields:
        raise ValueError("Intake is unavailable for this specialist.")
    answers = [{"question": field["question"], "kind": field["kind"],
        "answer": validate_answer(field, arguments.get("field_" + str(index)))}
        for index, field in enumerate(fields, 1)]
    api(SUBMISSIONS, "POST", params={"on_conflict": "organization_id,form_id,provider_call_id"}, data={
        "organization_id": workspace, "form_id": form_id, "provider_call_id": call["id"],
        "worker_name": form["worker_name"], "answers": answers}, prefer="resolution=ignore-duplicates,return=minimal")
    return "Intake saved to this call. No messages have been sent."


@router.post("/webhooks/worker-form/{oid}/{form_id}/{revision}", include_in_schema=False)
async def intake_webhook(oid: UUID, form_id: UUID, revision: UUID, request: Request,
                         authorization: str | None = Header(default=None)):
    workspace, form, version = str(oid), str(form_id), str(revision)
    if not os.getenv("VAPI_WEBHOOK_TOKEN") or not hmac.compare_digest(
        (authorization or "").encode(), ("Bearer " + form_token(workspace, form, version)).encode()
    ):
        raise HTTPException(401, "Invalid specialist form credential.")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 65536:
            raise HTTPException(413, "Specialist form payload too large.")
        body.extend(chunk)
    try:
        message = json.loads(body)["message"]
        call, tools = message["call"], message["toolCallList"]
        if (message["type"] != "tool-calls" or not isinstance(call, dict)
                or not isinstance(tools, list) or len(tools) > 4):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise HTTPException(400, "Invalid specialist form request.") from None
    results = []
    for tool in tools:
        if not isinstance(tool, dict) or not isinstance(tool.get("id"), str):
            continue
        item = {"toolCallId": tool["id"]}
        function = tool.get("function")
        try:
            if not isinstance(function, dict) or function.get("name") != "save_worker_intake":
                raise ValueError("This tool only saves specialist intake.")
            arguments = function.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    arguments = None
            if not isinstance(arguments, dict):
                raise ValueError("Invalid intake answers.")
            item["result"] = await run_in_threadpool(save_intake, workspace, form, version, call, arguments)
        except ValueError as exc:
            item["error"] = str(exc)[:240]
        except HTTPException:
            item["error"] = "Could not confirm that intake was saved. Try again or return to the operator."
        results.append(item)
    return {"results": results}
