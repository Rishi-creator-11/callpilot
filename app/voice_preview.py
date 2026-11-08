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
    """Voice calls are available to every workspace once the server switch is on.

    Cost is bounded per user and per workspace by the reserve_voice_preview quota, so the old
    per-workspace allowlist (VOICE_PREVIEW_WORKSPACES) is no longer consulted.
    """
    return enabled()


def daily_cap() -> int:
    """Shared daily cap on browser voice calls across all workspaces (VOICE_TEST_DAILY_CAP, default 100)."""
    try:
        return max(1, min(int(os.getenv("VOICE_TEST_DAILY_CAP", "100")), 5000))
    except ValueError:
        return 100


def reserve(workspace: str, user_id: str) -> bool:
    return rpc("reserve_voice_preview", {"p_organization_id": workspace, "p_user_id": user_id,
                                         "p_daily_cap": daily_cap()}) is True


def release(workspace: str, user_id: str) -> None:
    """A call that never started must not use up the person's allowance."""
    from datetime import datetime, timedelta, timezone
    since = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    try:
        api("voice_preview_attempts", "DELETE", params={
            "organization_id": "eq." + workspace, "user_id": "eq." + user_id, "created_at": "gte." + since},
            prefer="return=minimal")
    except Exception:  # noqa: BLE001 - best effort
        pass


def preview_token(oid: str) -> str:
    return hmac.new(os.environ["VAPI_WEBHOOK_TOKEN"].encode(),
                    ("callpilot-preview:" + oid).encode(), hashlib.sha256).hexdigest()


@router.get("/api/account/organizations/{oid}/voice-preview")
def status(oid: UUID, user=Depends(current_user)):
    role = membership(str(oid), user)
    return {"available": workspace_enabled(str(oid)) and role in ("owner", "admin")}


def preview_assistant(oid: str, *, knowledge_url: str | None = None,
                      knowledge_token: str | None = None, max_seconds: int = 90) -> dict:
    from app.identity import SPEECH_FIELDS, transcriber, voice_config

    business = org_or_404(oid)
    rows = api("agent_configs", params={"organization_id": f"eq.{oid}", "select": "agent_name,greeting," + SPEECH_FIELDS})
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
            "url": knowledge_url or f"{public_base()}/webhooks/vapi-preview/{oid}",
            "headers": {"Authorization": "Bearer " + (knowledge_token or preview_token(oid))},
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
        "voice": voice_config(agent, {"provider": "vapi", "voiceId": "Elliot", "version": 2}),
        "transcriber": transcriber(agent),
        "maxDurationSeconds": max_seconds,
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
    if not reserve(workspace, user["id"]):
        raise HTTPException(429, "Practice limit reached. Wait a few minutes, or try again tomorrow.")
    response.headers["Cache-Control"] = "no-store"
    try:
        return create_preview_room(assistant)
    except HTTPException:
        release(workspace, user["id"])
        raise


def create_preview_room(assistant: dict) -> dict:
    result = vapi("POST", "/call/web", body={"assistant": assistant, "roomDeleteOnUserLeaveEnabled": True}, key_env="VAPI_PUBLIC_KEY")
    return validated_room(result)


def validated_room(result) -> dict:
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
    safe_result = {"webCallUrl": room, "id": result.get("id")}
    if isinstance(transport.get("callToken"), str):
        safe_result["transport"] = {"callToken": transport["callToken"]}
    return safe_result


@router.post("/api/account/organizations/{oid}/voice-test")
def start_live_test(oid: UUID, response: Response, user=Depends(current_user)):
    """Talk to the real managed assistant, with its real tools. Actions taken in this call are real."""
    workspace = str(oid)
    membership(workspace, user, ("owner", "admin"))
    if not workspace_enabled(workspace):
        raise HTTPException(503, "Browser voice calls are not enabled.")
    rows = api("agent_configs", params={"organization_id": f"eq.{workspace}", "select": "provider_agent_id", "limit": 1})
    assistant_id = rows[0].get("provider_agent_id") if rows else None
    if not assistant_id:
        raise HTTPException(409, "Build your agent first.")
    from app.call_intel import owns_assistant
    if not owns_assistant(vapi("GET", "/assistant/" + assistant_id), workspace):
        raise HTTPException(409, "This assistant is not managed by this workspace.")
    if not reserve(workspace, user["id"]):
        raise HTTPException(429, "Test call limit reached. Wait a few minutes, or try again tomorrow.")
    response.headers["Cache-Control"] = "no-store"
    try:
        result = vapi("POST", "/call/web", body={
            "assistantId": assistant_id, "roomDeleteOnUserLeaveEnabled": True,
            "assistantOverrides": {"maxDurationSeconds": 150, "silenceTimeoutSeconds": 20}}, key_env="VAPI_PUBLIC_KEY")
        return validated_room(result)
    except HTTPException:
        release(workspace, user["id"])
        raise


@router.post("/webhooks/vapi-preview/{oid}")
async def preview_knowledge(oid: UUID, request: Request, authorization: str | None = Header(default=None)):
    workspace = str(oid)
    if not workspace_enabled(workspace) or not hmac.compare_digest(authorization or "", "Bearer " + preview_token(workspace)):
        raise HTTPException(401, "Invalid preview credential.")
    return await answer_preview_tools(workspace, request)


async def answer_preview_tools(workspace: str, request: Request):
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
