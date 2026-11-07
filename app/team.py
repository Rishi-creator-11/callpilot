"""Email-bound workspace invitations. Raw invitation tokens are returned once."""
from datetime import datetime, timedelta, timezone
import hashlib
import secrets
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from app.account import current_user, membership, rpc
from app.cloud import api
from app.common import public_base

router = APIRouter(prefix="/api/account", tags=["workspace-team"])
INVITE_FIELDS = "id,email,role,expires_at"
EMAIL = r"^[^\s@]+@[^\s@]+\.[^\s@]+$"


class Invitation(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    email: str = Field(min_length=3, max_length=254, pattern=EMAIL)
    role: Literal["viewer", "admin"] = "viewer"


class Acceptance(BaseModel):
    token: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")


def digest(token):
    return hashlib.sha256(token.encode("ascii")).hexdigest()


@router.get("/organizations/{oid}/invitations")
def invitations(oid: UUID, user=Depends(current_user)):
    membership(str(oid), user, ("owner", "admin"))
    return api("workspace_invitations", params={
        "organization_id": f"eq.{oid}", "revoked_at": "is.null", "accepted_at": "is.null",
        "expires_at": "gt." + datetime.now(timezone.utc).isoformat(), "select": INVITE_FIELDS,
        "order": "created_at.desc", "limit": 100,
    })


@router.post("/organizations/{oid}/invitations", status_code=201)
def invite(oid: UUID, body: Invitation, response: Response, user=Depends(current_user)):
    role = membership(str(oid), user, ("owner", "admin"))
    if body.role == "admin" and role != "owner":
        raise HTTPException(403, "Only workspace owners can invite an admin.")
    email = body.email.lower()
    if email == (user.get("email") or "").lower():
        raise HTTPException(422, "You already have access to this workspace.")
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    rows = api("workspace_invitations", "POST",
        params={"on_conflict": "organization_id,email,role", "select": INVITE_FIELDS},
        data={"organization_id": str(oid), "email": email, "role": body.role,
              "issued_by": user["id"], "token_hash": digest(token), "created_at": now.isoformat(),
              "expires_at": (now + timedelta(days=7)).isoformat(), "revoked_at": None,
              "accepted_at": None, "accepted_by": None},
        prefer="resolution=merge-duplicates,return=representation",
    )
    response.headers["Cache-Control"] = "no-store"
    return {**rows[0], "link": f"{public_base()}/app?mode=signup#invite={token}"}


@router.delete("/organizations/{oid}/invitations/{invite_id}", status_code=204)
def revoke_invitation(oid: UUID, invite_id: UUID, user=Depends(current_user)):
    role = membership(str(oid), user, ("owner", "admin"))
    params = {"organization_id": f"eq.{oid}", "id": f"eq.{invite_id}", "accepted_at": "is.null"}
    if role != "owner":
        params["role"] = "eq.viewer"
    api("workspace_invitations", "PATCH", params=params,
        data={"revoked_at": datetime.now(timezone.utc).isoformat()}, prefer="return=minimal")


@router.post("/invitations/accept")
def accept_invitation(body: Acceptance, user=Depends(current_user)):
    if not user.get("email"):
        raise HTTPException(403, "Sign in with a verified email address.")
    organization_id = rpc("accept_workspace_invitation", {
        "p_hash": digest(body.token), "p_user_id": user["id"], "p_email": user["email"].lower(),
    })
    if not organization_id:
        raise HTTPException(404, "This link is expired, revoked, or issued to a different email address.")
    return {"organization_id": organization_id}
