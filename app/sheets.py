"""Google Sheets lead export with least-privilege OAuth.

CallPilot asks only for the non-sensitive drive.file scope and creates its own spreadsheet,
so it can read and write only that file. Each captured lead appends one row, once per call.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import urllib.parse
from datetime import datetime, timedelta, timezone
from uuid import UUID

import httpx
from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

from app.account import current_user, membership, rpc
from app.calendar import REDIRECT, cipher, configured
from app.cloud import api, org_or_404

router = APIRouter(tags=["sheets"])
log = logging.getLogger("callpilot")
SCOPE = "https://www.googleapis.com/auth/drive.file"
STATE_PREFIX = "sheets."
STATE_COOKIE = "cp_sheets_state"
SHEET_TITLE = "Leads"
HEADERS = ["Captured at (UTC)", "Caller name", "Callback phone", "Interest", "Call ID"]


def connection(workspace: str) -> dict | None:
    rows = api("sheet_connections", params={
        "organization_id": "eq." + workspace,
        "select": "organization_id,refresh_token_ciphertext,spreadsheet_id,spreadsheet_url,connected_at",
    })
    return rows[0] if rows else None


def access_token(saved: dict) -> str:
    try:
        refresh = cipher().decrypt(saved["refresh_token_ciphertext"].encode()).decode()
    except InvalidToken:
        raise HTTPException(503, "Google Sheets token could not be decrypted")
    try:
        with httpx.Client(timeout=12) as client:
            response = client.post("https://oauth2.googleapis.com/token", data={
                "client_id": os.environ["GOOGLE_OAUTH_CLIENT_ID"],
                "client_secret": os.environ["GOOGLE_OAUTH_CLIENT_SECRET"],
                "grant_type": "refresh_token", "refresh_token": refresh,
            })
    except httpx.RequestError:
        raise HTTPException(503, "Google Sheets is unavailable")
    if response.status_code != 200:
        raise HTTPException(409, "Google Sheets authorization expired. Reconnect to continue.")
    return response.json()["access_token"]


def append_row(saved: dict, values: list[str]) -> None:
    token = access_token(saved)
    range_ = urllib.parse.quote(SHEET_TITLE + "!A:E", safe="")
    url = ("https://sheets.googleapis.com/v4/spreadsheets/" + urllib.parse.quote(saved["spreadsheet_id"], safe="")
           + "/values/" + range_ + ":append")
    try:
        with httpx.Client(timeout=15) as client:
            # RAW stores text literally, so a caller-supplied "=..." can never run as a formula.
            response = client.post(url, params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
                                   headers={"Authorization": "Bearer " + token}, json={"values": [values]})
    except httpx.RequestError:
        raise HTTPException(503, "Google Sheets is unavailable")
    if response.status_code != 200:
        raise HTTPException(502, "Google Sheets did not accept the row")


def create_spreadsheet(token: str, business: str) -> tuple[str, str]:
    header_row = {"values": [{"userEnteredValue": {"stringValue": text},
                              "userEnteredFormat": {"textFormat": {"bold": True}}} for text in HEADERS]}
    body = {"properties": {"title": (business[:80] + " – CallPilot leads")},
            "sheets": [{"properties": {"title": SHEET_TITLE, "gridProperties": {"frozenRowCount": 1}},
                        "data": [{"startRow": 0, "startColumn": 0, "rowData": [header_row]}]}]}
    with httpx.Client(timeout=20) as client:
        response = client.post("https://sheets.googleapis.com/v4/spreadsheets",
                               headers={"Authorization": "Bearer " + token}, json=body)
    response.raise_for_status()
    data = response.json()
    return data["spreadsheetId"], data["spreadsheetUrl"]


@router.get("/api/account/organizations/{oid}/sheets")
def sheets_status(oid: UUID, user=Depends(current_user)):
    workspace = str(oid)
    role = membership(workspace, user)
    saved = connection(workspace)
    manage = role in ("owner", "admin")
    return {"configured": configured(), "connected": bool(saved), "can_manage": manage,
            "spreadsheet_url": saved["spreadsheet_url"] if saved and manage else None,
            "connected_at": saved["connected_at"] if saved else None}


@router.post("/api/account/organizations/{oid}/sheets/connect")
def sheets_connect(oid: UUID, response: Response, user=Depends(current_user)):
    workspace = str(oid)
    membership(workspace, user, ("owner", "admin"))
    if not configured():
        raise HTTPException(503, "Google OAuth client credentials are not configured")
    state = STATE_PREFIX + secrets.token_urlsafe(32)
    api("oauth_states", "POST", data={
        "nonce": state, "organization_id": workspace, "created_by": user["id"],
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
    }, prefer="return=minimal")
    # Bind the state to this browser so a forged callback link cannot attach someone else's Google account.
    response.set_cookie(STATE_COOKIE, hashlib.sha256(state.encode()).hexdigest(), max_age=600,
                        httponly=True, secure=True, samesite="lax", path="/api/oauth/google")
    params = {"client_id": os.environ["GOOGLE_OAUTH_CLIENT_ID"], "redirect_uri": REDIRECT,
              "response_type": "code", "scope": SCOPE, "state": state, "access_type": "offline",
              "prompt": "consent", "include_granted_scopes": "false"}
    return {"url": "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)}


def handle_callback(code: str | None, state: str, error: str | None, request: Request) -> RedirectResponse:
    """Called from the shared Google OAuth callback for states that start with sheets."""
    def back(result: str) -> RedirectResponse:
        redirect = RedirectResponse("/app?sheets=" + result + "#connections")
        redirect.delete_cookie(STATE_COOKIE, path="/api/oauth/google")
        return redirect
    expected = hashlib.sha256(state.encode()).hexdigest()
    if not hmac.compare_digest(request.cookies.get(STATE_COOKIE, ""), expected):
        return back("error")
    if error:
        return back("access_denied" if error == "access_denied" else "error")
    if not code:
        return back("not_connected")
    valid = rpc("consume_oauth_state", {"p_nonce": state})
    if not valid:
        return back("expired")
    record = valid[0]
    try:  # The person who started the flow must still be an owner or admin of the workspace.
        membership(str(record["organization_id"]), {"id": str(record["created_by"])}, ("owner", "admin"))
    except HTTPException:
        return back("error")
    try:
        with httpx.Client(timeout=15) as client:
            response = client.post("https://oauth2.googleapis.com/token", data={
                "code": code, "client_id": os.environ["GOOGLE_OAUTH_CLIENT_ID"],
                "client_secret": os.environ["GOOGLE_OAUTH_CLIENT_SECRET"],
                "redirect_uri": REDIRECT, "grant_type": "authorization_code"})
        response.raise_for_status()
        tokens = response.json()
        refresh = tokens.get("refresh_token")
        if not refresh:
            return back("missing_refresh_token")
        if SCOPE not in (tokens.get("scope") or "").split():
            return back("missing_permission")
        workspace = record["organization_id"]
        spreadsheet_id, spreadsheet_url = create_spreadsheet(
            tokens["access_token"], org_or_404(workspace)["name"])
        api("sheet_connections", "POST", params={"on_conflict": "organization_id"}, data={
            "organization_id": workspace,
            "refresh_token_ciphertext": cipher().encrypt(refresh.encode()).decode(),
            "spreadsheet_id": spreadsheet_id, "spreadsheet_url": spreadsheet_url,
            "connected_by": record["created_by"],
        }, prefer="resolution=merge-duplicates,return=minimal")
        return back("connected")
    except (httpx.HTTPError, KeyError, ValueError, InvalidToken):
        return back("error")


@router.delete("/api/account/organizations/{oid}/sheets")
def sheets_disconnect(oid: UUID, user=Depends(current_user)):
    workspace = str(oid)
    membership(workspace, user, ("owner", "admin"))
    saved = connection(workspace)
    if saved:
        try:  # Best effort: ask Google to revoke the token. The spreadsheet stays in the owner's Drive.
            refresh = cipher().decrypt(saved["refresh_token_ciphertext"].encode()).decode()
            with httpx.Client(timeout=8) as client:
                client.post("https://oauth2.googleapis.com/revoke", data={"token": refresh})
        except (HTTPException, InvalidToken, httpx.HTTPError):
            pass
    api("oauth_states", "DELETE", params={"organization_id": "eq." + workspace, "nonce": "like." + STATE_PREFIX + "*"},
        prefer="return=minimal")
    api("sheet_connections", "DELETE", params={"organization_id": "eq." + workspace}, prefer="return=minimal")
    return {"connected": False}


@router.post("/api/account/organizations/{oid}/sheets/test-row")
def sheets_test_row(oid: UUID, user=Depends(current_user)):
    workspace = str(oid)
    membership(workspace, user, ("owner", "admin"))
    saved = connection(workspace)
    if not saved:
        raise HTTPException(409, "Connect Google Sheets first")
    append_row(saved, [datetime.now(timezone.utc).isoformat(timespec="seconds"), "CallPilot test",
                       "", "Test row. Safe to delete.", "test"])
    return {"added": True}


def export_lead(workspace: str, call_id: str, name: str, phone: str, interest: str) -> None:
    """Append one row for this call. Never raises, so lead capture is never blocked by Sheets."""
    try:
        saved = connection(workspace)
        if not saved:
            return
        marker = {"organization_id": workspace, "provider_call_id": call_id}
        inserted = api("sheet_exports", "POST", params={"on_conflict": "organization_id,provider_call_id"},
                       data=marker, prefer="resolution=ignore-duplicates,return=representation")
        if not inserted:
            return
        try:
            append_row(saved, [datetime.now(timezone.utc).isoformat(timespec="seconds"),
                               name, phone, interest, call_id])
        except Exception:
            api("sheet_exports", "DELETE", params={
                "organization_id": "eq." + workspace, "provider_call_id": "eq." + call_id},
                prefer="return=minimal")
            raise
    except Exception as exc:  # noqa: BLE001 - logged without caller details
        log.warning("sheets_export_failed workspace=%s error=%s", workspace, type(exc).__name__)
