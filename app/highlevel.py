"""Read-only HighLevel contact lookup with tenant-scoped credentials."""

import base64
import hmac
import os
import re
from datetime import datetime, timezone
from uuid import UUID

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field, SecretStr

from app.account import current_user, membership
from app.cloud import api

router = APIRouter(prefix="/api/account/organizations/{oid}/connections/highlevel")
TABLE = "workspace_provider_connections"
BASE_URL = "https://services.leadconnectorhq.com"
MANAGERS = ("owner", "admin")


def vault() -> Fernet:
    key = os.getenv("GOOGLE_TOKEN_ENCRYPTION_KEY")
    if not key:
        raise HTTPException(503, "Workspace credential storage is not configured.")
    derived = hmac.digest(key.encode(), b"callpilot/highlevel/private-integration/v1", "sha256")
    return Fernet(base64.urlsafe_b64encode(derived))


def filters(workspace: str) -> dict:
    return {"organization_id": "eq." + workspace, "provider": "eq.highlevel"}


def highlevel_get(path: str, token: str, params: dict | None = None) -> dict:
    try:
        response = httpx.get(
            BASE_URL + path,
            headers={"Authorization": "Bearer " + token, "Version": "v3", "Accept": "application/json"},
            params=params, timeout=10.0, follow_redirects=False,
        )
    except httpx.RequestError as exc:
        raise HTTPException(503, "HighLevel is temporarily unreachable.") from exc
    if response.status_code in (401, 403):
        raise HTTPException(422, "HighLevel denied access. Check the token, sub-account, and read permissions.")
    if response.status_code == 429:
        raise HTTPException(429, "HighLevel is limiting requests. Try again shortly.")
    if response.status_code == 404:
        raise HTTPException(404, "HighLevel could not find the requested record.")
    if response.status_code != 200:
        raise HTTPException(502, "HighLevel could not complete the request.")
    try:
        data = response.json()
    except ValueError as exc:
        raise HTTPException(502, "HighLevel returned an unreadable response.") from exc
    if not isinstance(data, dict):
        raise HTTPException(502, "HighLevel returned an unexpected response.")
    return data


class ConnectionInput(BaseModel):
    location_id: str = Field(pattern=r"^[A-Za-z0-9_-]{4,80}$")
    token: SecretStr


class ContactLookup(BaseModel):
    query: str = Field(min_length=3, max_length=160)


@router.get("")
def connection_status(oid: UUID, user=Depends(current_user)):
    workspace = str(oid)
    role = membership(workspace, user)
    rows = api(TABLE, params={**filters(workspace), "select": "account_name,connected_at", "limit": 1})
    return {"connected": bool(rows), "can_manage": role in MANAGERS, **(rows[0] if rows else {})}


@router.put("")
def connect(oid: UUID, body: ConnectionInput, user=Depends(current_user)):
    workspace = str(oid)
    membership(workspace, user, MANAGERS)
    token = body.token.get_secret_value().strip()
    if not re.fullmatch(r"[!-~]{15,1024}", token):
        raise HTTPException(422, "Enter a valid HighLevel private integration token.")
    cipher = vault()
    result = highlevel_get("/locations/" + body.location_id, token)
    location = result.get("location")
    if not isinstance(location, dict) or location.get("id") != body.location_id:
        raise HTTPException(422, "HighLevel did not confirm this sub-account.")
    # Check the contact scope using a reserved, non-deliverable address.
    highlevel_get("/contacts/search/duplicate", token,
                  {"locationId": body.location_id, "email": "callpilot-connection-check@example.invalid"})
    api(TABLE, "POST", params={"on_conflict": "organization_id,provider"}, data={
        "organization_id": workspace, "provider": "highlevel", "account_ref": body.location_id,
        "account_name": str(location.get("name") or "HighLevel sub-account")[:160],
        "token_ciphertext": cipher.encrypt(token.encode()).decode(),
        "connected_at": datetime.now(timezone.utc).isoformat(),
    }, prefer="resolution=merge-duplicates,return=minimal")
    return {"connected": True}


@router.delete("")
def disconnect(oid: UUID, user=Depends(current_user)):
    workspace = str(oid)
    membership(workspace, user, MANAGERS)
    api(TABLE, "DELETE", params=filters(workspace), prefer="return=minimal")
    return {"connected": False}


@router.post("/contacts/lookup")
def lookup(oid: UUID, body: ContactLookup, response: Response, user=Depends(current_user)):
    workspace = str(oid)
    membership(workspace, user, MANAGERS)
    query = body.query.strip()
    phone = re.sub(r"\D", "", query)
    if re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", query):
        field, value = "email", query
    elif re.fullmatch(r"[+() .0-9-]+", query) and 7 <= len(phone) <= 15:
        field, value = "number", "+" + phone if query.startswith("+") else phone
    else:
        raise HTTPException(422, "Enter an exact email address or phone number.")
    rows = api(TABLE, params={**filters(workspace), "select": "account_ref,token_ciphertext", "limit": 1})
    if not rows:
        raise HTTPException(409, "Connect HighLevel first.")
    account = rows[0]
    try:
        token = vault().decrypt(account["token_ciphertext"].encode()).decode()
    except InvalidToken as exc:
        raise HTTPException(503, "Reconnect HighLevel to refresh its stored credentials.") from exc
    result = highlevel_get("/contacts/search/duplicate", token, {"locationId": account["account_ref"], field: value})
    contact = result.get("contact")
    response.headers["Cache-Control"] = "no-store"
    if not isinstance(contact, dict) or contact.get("locationId") != account["account_ref"]:
        return {"contact": None}
    actual = str(contact.get("email" if field == "email" else "phone") or "")
    if (field == "email" and actual.casefold() != query.casefold()) or (field == "number" and re.sub(r"\D", "", actual) != phone):
        return {"contact": None}
    return {"contact": {key: str(contact.get(key) or "")[:160] for key in ("firstName", "lastName", "email", "phone")}}
