"""Expiring, revocable access to an explicitly shared read-only assistant demo."""

import hashlib
import hmac
import os
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from app.account import answer_question, current_user, membership, rpc
from app.cloud import api, org_or_404
from app.common import public_base

router = APIRouter(tags=["shareable-demo"])
OWNER_PATH = "/api/account/organizations/{oid}/demo-links"
TABLE = "workspace_demo_links"
MANAGERS = ("owner", "admin")
TEMPLATE = Path(__file__).resolve().parent.parent / "static" / "assistant-demo.html"
PRIVATE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex, nofollow"}


def signature(link_id: UUID) -> str:
    secret = os.getenv("SUPABASE_SECRET_KEY")
    if not secret:
        raise HTTPException(503, "Demo link signing is unavailable.")
    return hmac.new(secret.encode(), b"callpilot/demo-link/v1/" + link_id.bytes, hashlib.sha256).hexdigest()


def resolve_token(token: str) -> UUID:
    try:
        raw_id, supplied = token.split(".", 1)
        if len(raw_id) != 32 or len(supplied) != 64:
            raise ValueError
        link_id = UUID(hex=raw_id)
        if not hmac.compare_digest(supplied.encode(), signature(link_id).encode()):
            raise ValueError
        return link_id
    except ValueError as exc:
        raise HTTPException(404, "This demo link is invalid or expired.", headers=PRIVATE_HEADERS) from exc


def active_link(link_id: UUID) -> dict:
    rows = api(TABLE, params={
        "id": "eq." + str(link_id), "revoked_at": "is.null",
        "expires_at": "gt." + datetime.now(timezone.utc).isoformat(),
        "select": "organization_id,label", "limit": 1,
    })
    if not rows:
        raise HTTPException(404, "This demo link is invalid or expired.", headers=PRIVATE_HEADERS)
    return rows[0]


class LinkInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    label: str = Field(min_length=2, max_length=70)
    confirm_public_knowledge: bool


class DemoQuestion(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    question: str = Field(min_length=3, max_length=500)


@router.get(OWNER_PATH)
def list_links(oid: UUID, response: Response, user=Depends(current_user)):
    workspace = str(oid)
    membership(workspace, user, MANAGERS)
    response.headers["Cache-Control"] = "no-store"
    rows = api(TABLE, params={
        "organization_id": "eq." + workspace,
        "select": "id,label,created_at,expires_at,revoked_at,question_count",
        "order": "created_at.desc", "limit": 25,
    })
    for row in rows:
        active = not row["revoked_at"] and datetime.fromisoformat(row["expires_at"]) > datetime.now(timezone.utc)
        link_id = UUID(row["id"])
        row["url"] = public_base() + "/demo/" + link_id.hex + "." + signature(link_id) if active else None
    return {"links": rows}


@router.post(OWNER_PATH)
def create_link(oid: UUID, body: LinkInput, user=Depends(current_user)):
    workspace = str(oid)
    membership(workspace, user, MANAGERS)
    if not body.confirm_public_knowledge:
        raise HTTPException(422, "Confirm that approved answers may be shown to anyone with the link.")
    created = rpc("create_workspace_demo", {"p_workspace": workspace, "p_label": body.label})
    if not created:
        raise HTTPException(409, "Revoke an active link before creating another. Each workspace can have five active links.")
    return {"created": True}


@router.delete(OWNER_PATH + "/{link_id}")
def revoke_link(oid: UUID, link_id: UUID, user=Depends(current_user)):
    workspace = str(oid)
    membership(workspace, user, MANAGERS)
    api(TABLE, "PATCH", params={
        "organization_id": "eq." + workspace, "id": "eq." + str(link_id), "revoked_at": "is.null",
    }, data={"revoked_at": datetime.now(timezone.utc).isoformat()}, prefer="return=minimal")
    return {"revoked": True}


@router.get("/demo/{token}", response_class=HTMLResponse, include_in_schema=False)
def demo(token: str):
    try:
        link = active_link(resolve_token(token))
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        return HTMLResponse(TEMPLATE.read_text().replace("{{business}}", "Demo unavailable")
                            .replace("{{disabled}}", "disabled")
                            .replace("{{notice}}", "This link has expired, was revoked, or is invalid."),
                            status_code=404, headers=PRIVATE_HEADERS)
    business = org_or_404(link["organization_id"])
    return HTMLResponse(TEMPLATE.read_text().replace("{{disabled}}", "")
                        .replace("{{notice}}", "Ask a question about this business.")
                        .replace("{{business}}", escape(business["name"])),
                        headers=PRIVATE_HEADERS)


@router.post("/demo/{token}/ask", include_in_schema=False)
def ask_demo(token: str, body: DemoQuestion, response: Response):
    link_id = resolve_token(token)
    workspace = rpc("reserve_workspace_demo_question", {"p_id": str(link_id)})
    if not workspace:
        active_link(link_id)
        raise HTTPException(429, "This link has reached its usage limit. Try again later.", headers=PRIVATE_HEADERS)
    response.headers.update(PRIVATE_HEADERS)
    return {"answer": answer_question(workspace, body.question)["answer"]}
