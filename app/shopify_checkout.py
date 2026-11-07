"""Staff-reviewed order requests with a Shopify-hosted checkout."""

import re
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException

from app.account import current_user, membership
from app.cloud import api
from app.shopify_connection import MANAGERS, connected_store, exact_variant, graphql

router = APIRouter(prefix="/api/account/organizations/{oid}/orders/requests/{rid}/checkout")
TABLE = "order_requests"
DRAFT_FIELDS = "id status invoiceUrl order { id name displayFinancialStatus displayFulfillmentStatus }"
CREATE = """
mutation CreateOrderCheckout($input: DraftOrderInput!) {
  draftOrderCreate(input: $input) { draftOrder { id } userErrors { field message } }
}
"""
FIND = """
query RecoverOrderCheckout($search: String!) {
  draftOrders(first: 10, query: $search) {
    pageInfo { hasNextPage }
    nodes { id tags }
  }
}
"""
READ = "query ReadOrderCheckout($id: ID!) { draftOrder(id: $id) { " + DRAFT_FIELDS + " } }"
SCOPES = "query CheckoutPermissions { currentAppInstallation { accessScopes { handle } } }"


def order_key(oid: UUID, rid: UUID) -> dict:
    return {"organization_id": "eq." + str(oid), "id": "eq." + str(rid)}


def get_order(key: dict) -> dict:
    rows = api(TABLE, params={**key, "select":
        "status,sku,quantity,checkout_state,checkout_attempt,checkout_shop,checkout_draft_id", "limit": 1})
    if not rows:
        raise HTTPException(404, "Order request not found.")
    return rows[0]


def numeric_id(value: str, resource: str) -> str:
    match = re.fullmatch(r"gid://shopify/" + resource + r"/([0-9]+)", value or "")
    if not match:
        raise HTTPException(502, "Shopify did not return a valid " + resource + " ID.")
    return match[1]


def secure_checkout_url(value: str, domain: str) -> str:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if (parsed.scheme == "https" and parsed.port in (None, 443)
                and not parsed.username and not parsed.password and parsed.path.startswith("/")
                and (host == domain or host in ("shopify.com", "shop.app")
                     or host.endswith((".shopify.com", ".shop.app")))):
            return value
    except ValueError:
        pass
    raise HTTPException(502, "Shopify returned an unsupported checkout link. Open the draft in Shopify.")


def update_attempt(key: dict, attempt: str, updates: dict) -> None:
    rows = api(TABLE, "PATCH", params={**key, "checkout_attempt": "eq." + attempt,
        "checkout_state": "eq.pending"}, data=updates, prefer="return=representation")
    if not rows:
        current = get_order(key)
        if current["checkout_attempt"] != attempt or any(current.get(k) != v for k, v in updates.items()):
            raise HTTPException(409, "Checkout changed. Refresh the order and try again.")


def read_draft(domain: str, token: str, draft_id: str) -> dict:
    draft_number = numeric_id(draft_id, "DraftOrder")
    draft = graphql(domain, token, READ, {"id": draft_id}).get("draftOrder")
    if not draft or draft.get("id") != draft_id:
        raise HTTPException(409, "This draft is unavailable in Shopify. Review it in the original store.")
    order = draft.get("order")
    output = {"state": "created", "draft_status": draft.get("status"),
        "admin_url": "https://" + domain + "/admin/draft_orders/" + draft_number}
    if order:
        order_id = numeric_id(order.get("id"), "Order")
        output.update({"order_name": order.get("name"), "payment_status": order.get("displayFinancialStatus"),
            "fulfillment_status": order.get("displayFulfillmentStatus"),
            "admin_url": "https://" + domain + "/admin/orders/" + order_id})
    elif draft.get("status") != "COMPLETED" and draft.get("invoiceUrl"):
        try:
            output["checkout_url"] = secure_checkout_url(draft["invoiceUrl"], domain)
        except HTTPException as exc:
            output["message"] = exc.detail
    return output


def recover(key: dict, row: dict, domain: str, token: str) -> dict:
    marker = "callpilot-" + row["checkout_attempt"]
    connection = graphql(domain, token, FIND, {"search": "tag:" + marker}).get("draftOrders") or {}
    matches = [draft for draft in connection.get("nodes", []) if marker in draft.get("tags", [])]
    if len(matches) > 1 or (connection.get("pageInfo") or {}).get("hasNextPage"):
        raise HTTPException(409, "More than one Shopify draft may match. Review the drafts in Shopify.")
    if not matches:
        return {"state": "pending", "message": "Shopify has not confirmed a draft yet. Check again before creating another."}
    draft_id = matches[0].get("id")
    numeric_id(draft_id, "DraftOrder")
    update_attempt(key, row["checkout_attempt"], {"checkout_state": "created", "checkout_draft_id": draft_id})
    return read_draft(domain, token, draft_id)


@router.post("")
def start_checkout(oid: UUID, rid: UUID, user=Depends(current_user)):
    membership(str(oid), user, MANAGERS)
    key = order_key(oid, rid)
    row = get_order(key)
    if row["status"] != "approved":
        raise HTTPException(409, "Approve the order request before creating a checkout.")
    state = row["checkout_state"]
    domain, token = connected_store(oid, row["checkout_shop"] if state in ("created", "pending") else None)
    if state == "created":
        return read_draft(domain, token, row["checkout_draft_id"])
    if state == "pending":
        return recover(key, row, domain, token)
    installation = graphql(domain, token, SCOPES).get("currentAppInstallation") or {}
    scopes = {scope.get("handle") for scope in installation.get("accessScopes", [])}
    if not {"write_draft_orders", "read_orders"}.issubset(scopes):
        raise HTTPException(409, "Add write_draft_orders and read_orders to the installed Shopify app.")
    variant = exact_variant(domain, token, row["sku"])
    if not variant:
        raise HTTPException(409, "No active Shopify variant matches this request's SKU.")
    variant_id = variant.get("id")
    numeric_id(variant_id, "ProductVariant")
    attempt = str(uuid4())
    claimed = api(TABLE, "PATCH", params={**key, "status": "eq.approved",
        "or": "(checkout_state.is.null,checkout_state.eq.failed)"}, data={
        "checkout_state": "pending", "checkout_attempt": attempt, "checkout_shop": domain,
    }, prefer="return=representation")
    if not claimed:
        raise HTTPException(409, "Another team member changed this request. Refresh to see its checkout state.")
    result = graphql(domain, token, CREATE, {"input": {
        "lineItems": [{"variantId": variant_id, "quantity": row["quantity"]}],
        "tags": ["callpilot-" + attempt],
    }}).get("draftOrderCreate") or {}
    draft_id = (result.get("draftOrder") or {}).get("id")
    if not draft_id and result.get("userErrors"):
        update_attempt(key, attempt, {"checkout_state": "failed"})
        raise HTTPException(422, "Shopify declined the draft. Check the product and quantity before retrying.")
    numeric_id(draft_id, "DraftOrder")
    update_attempt(key, attempt, {"checkout_state": "created", "checkout_draft_id": draft_id})
    return read_draft(domain, token, draft_id)
