"""Owner-controlled provisioning of an inbound-only Vapi number."""

import os
import re
from datetime import datetime, timezone
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.account import current_user, membership, rpc
from app.call_intel import agent_config, owns_assistant, vapi
from app.cloud import api

router = APIRouter(prefix="/api/account/organizations/{oid}/phone/provision", tags=["phone"])
TABLE = "phone_provision_requests"


class ProvisionRequest(BaseModel):
    area_code: str = Field(pattern=r"^[2-9][0-9]{2}$")
    confirm: bool


def available(oid: str) -> bool:
    allowed = {item.strip() for item in os.getenv("MANAGED_PHONE_PROVISION_WORKSPACES", "").split(",")}
    return (os.getenv("MANAGED_PHONE_PROVISION_ENABLED", "").lower() == "true"
            and oid in allowed and bool(os.getenv("VAPI_API_KEY")))


def get_job(oid: str) -> dict | None:
    rows = api(TABLE, params={"organization_id": "eq." + oid,
        "select": "request_id,area_code,status,updated_at", "limit": 1})
    return rows[0] if rows else None


def finish(oid: str, request_id: str, provider: dict) -> dict:
    number = provider.get("number") or ""
    provider_id = provider.get("id") or ""
    if (provider.get("provider") != "vapi" or not isinstance(number, str)
            or not re.fullmatch(r"\+1[2-9][0-9]{9}", number)
            or not isinstance(provider_id, str) or not re.fullmatch(r"[A-Za-z0-9-]{10,100}", provider_id)):
        raise HTTPException(502, "The provider has not confirmed the requested number. Check the existing request.")
    if rpc("finish_managed_phone_provision", {
        "p_workspace": oid, "p_request": request_id, "p_phone_id": provider_id, "p_number": number,
    }) is not True:
        raise HTTPException(409, "The workspace already has another number. Review the pending request before retrying.")
    return {"status": "provisioned", "number": number}


def reconcile(oid: str, job: dict) -> dict:
    if job["status"] == "provisioned":
        return {"status": "provisioned"}
    if job["status"] == "failed":
        return {"status": "failed"}
    results = vapi("GET", "/phone-number", params={"limit": 1000})
    if not isinstance(results, list):
        raise HTTPException(502, "The provider did not return the number list.")
    marker = "CP-" + job["request_id"]
    matches = [row for row in results if isinstance(row, dict) and row.get("name") == marker]
    if len(matches) == 1:
        return finish(oid, job["request_id"], matches[0])
    if len(matches) > 1:
        raise HTTPException(409, "Multiple numbers matched this request. Provider reconciliation is required.")
    return {"status": "pending"}


@router.get("")
def status(oid: UUID, user=Depends(current_user)):
    workspace = str(oid)
    role = membership(workspace, user)
    if role != "owner":
        return {"available": False, "job": None}
    job = get_job(workspace)
    if job:
        job.pop("request_id", None)
    return {"available": available(workspace), "job": job}


@router.post("/reconcile")
def reconcile_number(oid: UUID, user=Depends(current_user)):
    workspace = str(oid)
    membership(workspace, user, ("owner",))
    job = get_job(workspace)
    if not job:
        raise HTTPException(404, "No number request exists.")
    return reconcile(workspace, job)


@router.post("")
def provision(oid: UUID, body: ProvisionRequest, user=Depends(current_user)):
    workspace = str(oid)
    membership(workspace, user, ("owner",))
    if not available(workspace):
        raise HTTPException(409, "Self-service number provisioning is unavailable for this workspace.")
    if not body.confirm:
        raise HTTPException(422, "Confirm that you want to reserve an inbound number.")
    job = get_job(workspace)
    if job and job["status"] != "failed":
        return reconcile(workspace, job)
    config = agent_config(workspace)
    assistant_id = config.get("provider_agent_id")
    if config.get("provider_phone_id") or not assistant_id:
        raise HTTPException(409, "An existing number is linked or the workspace has no managed assistant.")
    if not owns_assistant(vapi("GET", "/assistant/" + assistant_id), workspace):
        raise HTTPException(409, "Connect a CallPilot-managed assistant before reserving a number.")
    request_id = str(uuid4())
    if rpc("begin_managed_phone_provision", {
        "p_workspace": workspace, "p_request": request_id, "p_area": body.area_code,
    }) is not True:
        prior = get_job(workspace)
        if prior:
            return reconcile(workspace, prior)
        raise HTTPException(409, "A number request is already active.")
    try:
        response = httpx.post("https://api.vapi.ai/phone-number", timeout=18.0,
            headers={"Authorization": "Bearer " + os.environ["VAPI_API_KEY"]},
            json={"provider": "vapi", "name": "CP-" + request_id, "numberDesiredAreaCode": body.area_code})
    except httpx.RequestError as exc:
        raise HTTPException(502, "The provider did not confirm the request. Use Check pending number before trying again.") from exc
    if response.status_code in (400, 401, 403, 404, 422, 429):
        api(TABLE, "PATCH", params={"organization_id": "eq." + workspace, "request_id": "eq." + request_id,
                                   "status": "eq.pending"},
            data={"status": "failed", "updated_at": datetime.now(timezone.utc).isoformat()}, prefer="return=minimal")
        raise HTTPException(409, "The provider declined the reservation. Check the area code and your Vapi number allowance.")
    if response.status_code not in (200, 201):
        raise HTTPException(502, "The provider did not confirm the request. Use Check pending number before trying again.")
    try:
        provider = response.json()
    except ValueError as exc:
        raise HTTPException(502, "The provider response could not be read. Check the pending number.") from exc
    if not isinstance(provider, dict):
        raise HTTPException(502, "The provider response could not be read. Check the pending number.")
    return finish(workspace, request_id, provider)
