"""Short browser calls with a transient, read-only receptionist."""
import hashlib
import hmac
import json
import os
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool

from app.account import answer_question, current_user, membership, rpc
from app.call_intel import vapi
from app.cloud import api, org_or_404, receptionist_prompt
from app.common import public_base

router = APIRouter(tags=["voice-preview"])


def enabled() -> bool:
    return (os.getenv("VOICE_PREVIEW_ENABLED", "").lower() == "true"
            and bool(os.getenv("VAPI_API_KEY"))
            and bool(os.getenv("VAPI_WEBHOOK_TOKEN")))


def workspace_enabled(oid: str) -> bool:
    allowlist = {item.strip() for item in os.getenv("VOICE_PREVIEW_WORKSPACES", "").split(",") if item.strip()}
    return enabled() and (not allowlist or oid in allowlist)


def preview_token(oid: str) -> str:
    return hmac.new(os.environ["VAPI_WEBHOOK_TOKEN"].encode(),
                    ("callpilot-preview:" + oid).encode(), hashlib.sha256).hexdigest()


@router.get("/api/account/organizations/{oid}/voice-preview")
def status(oid: UUID, user=Depends(current_user)):
    role = membership(str(oid), user)
    return {"available": workspace_enabled(str(oid)) and role in ("owner", "admin")}


def preview_assistant(oid: str) -> dict:
    business = org_or_404(oid)
    rows = api("agent_configs", params={"organization_id": f"eq.{oid}", "select": "agent_name,greeting"})
    if not rows:
        raise HTTPException(409, "Set up your receptionist first.")
    agent = rows[0]
    knowledge = {
        "type": "function", "async": False,
        "function": {
            "name": "lookup_approved_knowledge", "strict": True,
            "description": "Find an approved business answer for a question.",
            "parameters": {
                "type": "object", "required": ["question"], "additionalProperties": False,
                "properties": {"question": {"type": "string"}},
            },
        },
        "server": {
            "url": f"{public_base()}/webhooks/vapi-preview/{oid}",
            "headers": {"Authorization": "Bearer " + preview_token(oid)},
            "timeoutSeconds": 15,
        },
    }
    prompt = receptionist_prompt(oid, business["name"], agent["agent_name"], preview=True)
    return {
        "name": "CallPilot browser practice",
        "firstMessage": agent["greeting"],
        "firstMessageMode": "assistant-speaks-first",
        "model": {
            "provider": "openai", "model": "gpt-4o-mini", "temperature": 0,
            "messages": [{"role": "system", "content": prompt}],
            "tools": [knowledge, {"type": "endCall"}],
        },
        "voice": {"provider": "vapi", "voiceId": "Elliot", "version": 2, "language": "auto"},
        "transcriber": {"provider": "deepgram", "model": "nova-3", "language": "multi"},
        "maxDurationSeconds": 90,
        "silenceTimeoutSeconds": 25,
        "artifactPlan": {"recordingEnabled": False, "videoRecordingEnabled": False,
                         "transcriptPlan": {"enabled": False}},
        "clientMessages": ["transcript"],
        "serverMessages": [],
    }


@router.post("/api/account/organizations/{oid}/voice-preview")
def start_preview(oid: UUID, response: Response, user=Depends(current_user)):
    workspace = str(oid)
    membership(workspace, user, ("owner", "admin"))
    if not workspace_enabled(workspace):
        raise HTTPException(503, "Browser voice practice is not enabled.")
    assistant = preview_assistant(workspace)
    if rpc("reserve_voice_preview", {"p_organization_id": workspace, "p_user_id": user["id"]}) is not True:
        raise HTTPException(429, "Practice limit reached. Wait a few minutes, or try again tomorrow.")
    result = vapi("POST", "/call/web", body={"assistant": assistant, "roomDeleteOnUserLeaveEnabled": True})
    if not isinstance(result, dict):
        raise HTTPException(502, "The voice provider returned an invalid practice response.")
    transport = result.get("transport")
    if not isinstance(transport, dict):
        transport = {}
    room = result.get("webCallUrl") or transport.get("callUrl")
    try:
        url = urlsplit(room if isinstance(room, str) else "")
        host = url.hostname or ""
        if (url.scheme != "https" or not host.endswith(".daily.co") or not url.path
                or url.username or url.password or url.port is not None):
            raise ValueError
    except ValueError:
        raise HTTPException(502, "The voice provider did not return a valid practice room.") from None
    response.headers["Cache-Control"] = "no-store"
    safe_result = {"webCallUrl": room, "id": result.get("id")}
    if isinstance(transport.get("callToken"), str):
        safe_result["transport"] = {"callToken": transport["callToken"]}
    return safe_result


@router.post("/webhooks/vapi-preview/{oid}")
async def preview_knowledge(oid: UUID, request: Request, authorization: str | None = Header(default=None)):
    workspace = str(oid)
    if not workspace_enabled(workspace) or not hmac.compare_digest(authorization or "", "Bearer " + preview_token(workspace)):
        raise HTTPException(401, "Invalid preview credential.")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 65536:
            raise HTTPException(413, "Preview payload too large.")
        body.extend(chunk)
    try:
        message = json.loads(body)["message"]
        calls = message["toolCallList"]
        if message["type"] != "tool-calls" or not isinstance(calls, list) or len(calls) > 3:
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise HTTPException(400, "Invalid preview request.") from None
    results = []
    for call in calls:
        if not isinstance(call, dict) or not isinstance(call.get("id"), str):
            continue
        item = {"toolCallId": call["id"], "error": "Only approved knowledge is available during practice."}
        function = call.get("function")
        if isinstance(function, dict) and function.get("name") == "lookup_approved_knowledge":
            arguments = function.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    arguments = None
            question = arguments.get("question") if isinstance(arguments, dict) else None
            if isinstance(question, str) and 1 <= len(question) <= 500:
                try:
                    answer = await run_in_threadpool(answer_question, workspace, question)
                    item = {"toolCallId": call["id"], "result": " ".join(answer["answer"].split())}
                except HTTPException:
                    item["error"] = "Approved knowledge is temporarily unavailable."
        results.append(item)
    return {"results": results}
