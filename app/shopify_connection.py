"""Owner-managed, read-only Shopify inventory connection."""

import json
import re
from datetime import datetime, timezone
from uuid import UUID

import httpx
from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, SecretStr

from app.account import current_user, membership
from app.cloud import api
from app.highlevel import TABLE, MANAGERS, vault

router = APIRouter(prefix="/api/account/organizations/{oid}/connections/shopify")
VERSION = "2026-10"
SHOP = re.compile(r"[a-z0-9][a-z0-9-]{0,62}\.myshopify\.com")
SHOP_QUERY = """
query Connection {
  shop { name myshopifyDomain }
  currentAppInstallation { accessScopes { handle } }
}
"""
STOCK_QUERY = """
query StockBySku($search: String!) {
  productVariants(first: 20, query: $search) {
    pageInfo { hasNextPage }
    nodes { id sku title inventoryQuantity inventoryItem { tracked } product { title status } }
  }
}
"""


def filters(oid: UUID) -> dict:
    return {"organization_id": "eq." + str(oid), "provider": "eq.shopify"}


def request_json(domain: str, path: str, **kwargs) -> dict:
    if not SHOP.fullmatch(domain):
        raise HTTPException(422, "Enter your store’s myshopify.com domain.")
    try:
        response = httpx.post("https://" + domain + path, timeout=6.0, follow_redirects=False, **kwargs)
    except httpx.RequestError as exc:
        raise HTTPException(503, "Shopify is temporarily unreachable.") from exc
    if response.status_code in (400, 401, 403):
        raise HTTPException(422, "Shopify declined access. Check the installed app and its credentials.")
    if response.status_code == 429:
        raise HTTPException(429, "Shopify is limiting requests. Try again shortly.")
    if response.status_code != 200:
        raise HTTPException(502, "Shopify could not complete the request.")
    try:
        result = response.json()
    except ValueError as exc:
        raise HTTPException(502, "Shopify returned an unreadable response.") from exc
    if not isinstance(result, dict):
        raise HTTPException(502, "Shopify returned an unexpected response.")
    return result


def access_token(domain: str, credentials: dict) -> str:
    result = request_json(domain, "/admin/oauth/access_token", data={
        "grant_type": "client_credentials", **credentials,
    })
    token = result.get("access_token")
    if not isinstance(token, str) or not token:
        raise HTTPException(502, "Shopify did not issue an access token.")
    return token


def graphql(domain: str, token: str, query: str, variables: dict | None = None) -> dict:
    result = request_json(domain, "/admin/api/" + VERSION + "/graphql.json",
        headers={"X-Shopify-Access-Token": token},
        json={"query": query, "variables": variables or {}})
    data = result.get("data")
    if result.get("errors") or not isinstance(data, dict):
        raise HTTPException(502, "Shopify could not complete this operation. Check the app’s permissions.")
    return data


class StoreInput(BaseModel):
    domain: str = Field(max_length=80)
    client_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,128}$")
    client_secret: SecretStr


class SkuInput(BaseModel):
    sku: str = Field(min_length=2, max_length=60, pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


@router.get("")
def status(oid: UUID, user=Depends(current_user)):
    role = membership(str(oid), user)
    rows = api(TABLE, params={**filters(oid), "select": "account_name,connected_at", "limit": 1})
    return {"connected": bool(rows), "can_manage": role in MANAGERS, **(rows[0] if rows else {})}


@router.put("")
def connect(oid: UUID, body: StoreInput, user=Depends(current_user)):
    workspace = str(oid)
    membership(workspace, user, MANAGERS)
    domain = body.domain.lower().strip()
    secret = body.client_secret.get_secret_value().strip()
    if not re.fullmatch(r"[!-~]{8,512}", secret):
        raise HTTPException(422, "Enter a valid Shopify client secret.")
    cipher = vault("shopify")
    credentials = {"client_id": body.client_id, "client_secret": secret}
    data = graphql(domain, access_token(domain, credentials), SHOP_QUERY)
    shop = data.get("shop") or {}
    if str(shop.get("myshopifyDomain") or "").lower() != domain:
        raise HTTPException(422, "Shopify did not confirm this store domain.")
    installation = data.get("currentAppInstallation") or {}
    scopes = {scope.get("handle") for scope in installation.get("accessScopes", [])}
    if not {"read_products", "read_inventory"}.issubset(scopes):
        raise HTTPException(422, "Install the Shopify app with read_products and read_inventory.")
    api(TABLE, "POST", params={"on_conflict": "organization_id,provider"}, data={
        "organization_id": workspace, "provider": "shopify", "account_ref": domain,
        "account_name": str(shop.get("name") or domain)[:160],
        "token_ciphertext": cipher.encrypt(json.dumps(credentials).encode()).decode(),
        "connected_at": datetime.now(timezone.utc).isoformat(),
    }, prefer="resolution=merge-duplicates,return=minimal")
    return {"connected": True}


@router.delete("")
def disconnect(oid: UUID, user=Depends(current_user)):
    membership(str(oid), user, MANAGERS)
    api(TABLE, "DELETE", params=filters(oid), prefer="return=minimal")
    return {"connected": False}


@router.post("/inventory")
def inventory(oid: UUID, body: SkuInput, user=Depends(current_user)):
    membership(str(oid), user, MANAGERS)
    return {"product": lookup_inventory(oid, body.sku)}


def connected_store(oid: UUID, expected_domain: str | None = None) -> tuple[str, str]:
    rows = api(TABLE, params={**filters(oid), "select": "account_ref,token_ciphertext", "limit": 1})
    if not rows:
        raise HTTPException(409, "Connect Shopify first.")
    account = rows[0]
    domain = account["account_ref"]
    if expected_domain and domain != expected_domain:
        raise HTTPException(409, "Reconnect the original Shopify store to manage this checkout.")
    try:
        credentials = json.loads(vault("shopify").decrypt(account["token_ciphertext"].encode()))
    except (InvalidToken, ValueError) as exc:
        raise HTTPException(503, "Reconnect Shopify to refresh its stored credentials.") from exc
    return domain, access_token(domain, credentials)


def exact_variant(domain: str, token: str, sku: str) -> dict | None:
    data = graphql(domain, token, STOCK_QUERY, {"search": "sku:" + json.dumps(sku)})
    connection = data.get("productVariants") or {}
    variants = [
        row for row in connection.get("nodes", [])
        if str(row.get("sku") or "").casefold() == sku.casefold()
        and (row.get("product") or {}).get("status") == "ACTIVE"
    ]
    if len(variants) > 1 or (connection.get("pageInfo") or {}).get("hasNextPage"):
        raise HTTPException(409, "The SKU search returned multiple possible variants. Use a unique SKU.")
    return variants[0] if variants else None


def lookup_inventory(oid: UUID, sku: str) -> dict | None:
    domain, token = connected_store(oid)
    row = exact_variant(domain, token, sku)
    if not row:
        return None
    tracked = (row.get("inventoryItem") or {}).get("tracked") is True
    return {"sku": row["sku"], "name": row["product"]["title"], "variant": row["title"],
            "quantity": row.get("inventoryQuantity") if tracked else None}
