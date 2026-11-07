"""Self-service account deletion with guards for anything that could strand money or other people."""
from __future__ import annotations

import os

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.account import SUPA, current_user
from app.billing import account as billing_account
from app.cloud import api

router = APIRouter(prefix="/api/account", tags=["account"])
LIVE_BILLING = ("active", "trialing", "past_due")


class DeleteAccount(BaseModel):
    confirm: str


def blockers_for(user_id: str, owned: list[str]) -> list[str]:
    reasons: list[str] = []
    for workspace in owned:
        others = api("organization_members", params={
            "organization_id": "eq." + workspace, "user_id": "neq." + user_id,
            "select": "user_id", "limit": 1})
        if others:
            reasons.append("Remove other team members from your workspaces or give them ownership first.")
        billing = billing_account(workspace)
        if billing and billing.get("status") in LIVE_BILLING:
            reasons.append("Cancel your subscription from Plan & billing first.")
        config = api("agent_configs", params={
            "organization_id": "eq." + workspace, "select": "provider_phone_id", "limit": 1})
        if config and config[0].get("provider_phone_id"):
            reasons.append("Release or unlink your business phone number first.")
    return list(dict.fromkeys(reasons))


def delete_auth_user(user_id: str) -> None:
    secret = os.getenv("SUPABASE_SECRET_KEY", "")
    if not secret or not SUPA():
        raise HTTPException(503, "Account deletion is not configured")
    try:
        with httpx.Client(timeout=15) as client:
            response = client.delete(SUPA() + "/auth/v1/admin/users/" + user_id,
                                     headers={"apikey": secret, "Authorization": "Bearer " + secret})
    except httpx.RequestError:
        raise HTTPException(503, "Account service unavailable")
    if response.status_code not in (200, 204, 404):
        raise HTTPException(502, "Your data was removed but the sign-in could not be deleted. Contact support.")


@router.delete("/me")
def delete_account(body: DeleteAccount, user=Depends(current_user)):
    if body.confirm != "DELETE":
        raise HTTPException(422, "Type DELETE to confirm.")
    user_id = user["id"]
    memberships = api("organization_members", params={"user_id": "eq." + user_id, "select": "organization_id,role"})
    owned = [m["organization_id"] for m in memberships if m["role"] == "owner"]
    reasons = blockers_for(user_id, owned)
    if reasons:
        raise HTTPException(409, " ".join(reasons))
    for workspace in owned:  # Every table that references a workspace cascades on delete.
        api("organizations", "DELETE", params={"id": "eq." + workspace}, prefer="return=minimal")
    api("organization_members", "DELETE", params={"user_id": "eq." + user_id}, prefer="return=minimal")
    delete_auth_user(user_id)
    return {"deleted": True}
