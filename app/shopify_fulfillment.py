"""Record physical shipments for paid CallPilot orders in the original Shopify store."""

import hashlib
import json
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.account import current_user, membership
from app.cloud import api
from app.shopify_checkout import get_order, numeric_id, order_key
from app.shopify_connection import MANAGERS, connected_store, graphql

router = APIRouter(prefix="/api/account/organizations/{oid}/orders/requests/{rid}/fulfillment")
TABLE = "shopify_shipment_attempts"
SCOPE = "write_merchant_managed_fulfillment_orders"
ORDER = """
query PaidOrder($draft: ID!) {
  draftOrder(id: $draft) {
    order { id displayFinancialStatus cancelledAt }
  }
  currentAppInstallation { accessScopes { handle } }
}
"""
GROUP_FIELDS = """
  id orderId status requestStatus updatedAt
  assignedLocation { name }
  deliveryMethod { methodType }
  supportedActions { action }
  lineItems(first: 20) {
    pageInfo { hasNextPage }
    nodes { id productTitle sku remainingQuantity requiresShipping }
  }
"""
GROUPS = """
query OrderShipmentGroups($order: ID!) {
  order(id: $order) { fulfillmentOrders(first: 15) {
    pageInfo { hasNextPage }
    nodes { """ + GROUP_FIELDS + """ }
  } }
}
"""
GROUP = "query ShipmentGroup($id: ID!) { fulfillmentOrder(id: $id) { " + GROUP_FIELDS + " } }"
TRACKING = """
query RecordedShipment($id: ID!) {
  fulfillmentOrder(id: $id) {
    orderId
    fulfillments(first: 30) {
      pageInfo { hasNextPage }
      nodes { id status trackingInfo { number company } }
    }
  }
}
"""
CREATE = """
mutation RecordShipment($input: FulfillmentInput!) {
  fulfillmentCreate(fulfillment: $input) {
    fulfillment { id status }
    userErrors { field message }
  }
}
"""


class Shipment(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    fulfillment_order_id: str = Field(pattern=r"^gid://shopify/FulfillmentOrder/[0-9]+$", max_length=100)
    snapshot: str = Field(pattern=r"^[a-f0-9]{64}$")
    tracking_number: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{2,79}$")
    carrier: str = Field(min_length=2, max_length=80)
    confirm: bool


class Recheck(BaseModel):
    fulfillment_order_id: str = Field(pattern=r"^gid://shopify/FulfillmentOrder/[0-9]+$", max_length=100)


def provider_order(oid: UUID, rid: UUID) -> tuple[str, str, dict, bool]:
    row = get_order(order_key(oid, rid))
    if row["status"] != "approved" or row["checkout_state"] != "created":
        raise HTTPException(409, "Create checkout before recording a shipment.")
    domain, token = connected_store(oid, row["checkout_shop"])
    data = graphql(domain, token, ORDER, {"draft": row["checkout_draft_id"]})
    order = (data.get("draftOrder") or {}).get("order")
    if not order:
        raise HTTPException(409, "Shopify has not created an order from this checkout yet.")
    numeric_id(order.get("id"), "Order")
    scopes = {item["handle"] for item in (data.get("currentAppInstallation") or {}).get("accessScopes", [])}
    return domain, token, order, SCOPE in scopes


def shipment_key(oid: UUID, rid: UUID, group_id: str | None = None) -> dict:
    params = {"organization_id": "eq." + str(oid), "order_request_id": "eq." + str(rid)}
    if group_id:
        params["fulfillment_order_id"] = "eq." + group_id
    return params


def attempts(oid: UUID, rid: UUID) -> list:
    return api(TABLE, params={**shipment_key(oid, rid),
        "select": "fulfillment_order_id,status,tracking_number,carrier,fulfillment_id", "limit": 100})


def shipping_items(group: dict) -> list:
    if (group.get("status") not in ("OPEN", "IN_PROGRESS") or group.get("requestStatus") != "UNSUBMITTED"
            or (group.get("deliveryMethod") or {}).get("methodType") != "SHIPPING"
            or not any(item["action"] == "CREATE_FULFILLMENT" for item in group.get("supportedActions", []))):
        return []
    connection = group["lineItems"]
    remaining = [item for item in connection["nodes"] if item["remainingQuantity"] > 0]
    if connection["pageInfo"]["hasNextPage"] or any(not item["requiresShipping"] for item in remaining):
        return []
    return remaining


def snapshot(group: dict, items: list) -> str:
    payload = [group["id"], group["updatedAt"], group["assignedLocation"]["name"],
               sorted((item["id"], item["remainingQuantity"]) for item in items)]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


@router.get("")
def list_shipments(oid: UUID, rid: UUID, user=Depends(current_user)):
    membership(str(oid), user, MANAGERS)
    domain, token, order, can_write = provider_order(oid, rid)
    history = [attempt for attempt in attempts(oid, rid) if attempt["status"] in ("pending", "recorded")]
    if not can_write:
        return {"options": [], "shipments": history, "message":
            "Grant write_merchant_managed_fulfillment_orders to record shipments here."}
    if order.get("cancelledAt") or order["displayFinancialStatus"] != "PAID":
        return {"options": [], "shipments": history, "message":
            "Only paid, active Shopify orders can be shipped from CallPilot."}
    connection = (graphql(domain, token, GROUPS, {"order": order["id"]}).get("order") or {}).get("fulfillmentOrders")
    if not connection or connection["pageInfo"]["hasNextPage"]:
        return {"options": [], "shipments": history, "message": "Review these fulfillment groups in Shopify."}
    busy = {item["fulfillment_order_id"] for item in history}
    options = []
    for group in connection["nodes"]:
        items = shipping_items(group)
        if not items or group["id"] in busy or group["orderId"] != order["id"]:
            continue
        options.append({"id": group["id"], "location": group["assignedLocation"]["name"],
            "snapshot": snapshot(group, items),
            "items": [{"name": item["productTitle"], "sku": item["sku"],
                       "quantity": item["remainingQuantity"]} for item in items]})
    return {"options": options, "shipments": history, "message":
        "" if options or history else "No eligible shipping groups. Open the order in Shopify for other fulfillment options."}


def update_job(key: dict, attempt_id: str, data: dict) -> None:
    api(TABLE, "PATCH", params={**key, "status": "eq.pending", "attempt_id": "eq." + attempt_id},
        data=data, prefer="return=minimal")


@router.post("")
def record_shipment(oid: UUID, rid: UUID, body: Shipment, user=Depends(current_user)):
    membership(str(oid), user, MANAGERS)
    if not body.confirm:
        raise HTTPException(422, "Confirm that these items are packed and ready to ship.")
    domain, token, order, can_write = provider_order(oid, rid)
    if not can_write or order.get("cancelledAt") or order["displayFinancialStatus"] != "PAID":
        raise HTTPException(409, "This paid shipment or Shopify permission is no longer available.")
    group = graphql(domain, token, GROUP, {"id": body.fulfillment_order_id}).get("fulfillmentOrder")
    if not group or group.get("orderId") != order["id"]:
        raise HTTPException(404, "Shipment group not found on this order.")
    items = shipping_items(group)
    if not items or snapshot(group, items) != body.snapshot:
        raise HTTPException(409, "The shipment changed in Shopify. Refresh and review the items again.")
    key = shipment_key(oid, rid, group["id"])
    attempt = str(uuid4())
    payload = {"attempt_id": attempt, "carrier": body.carrier, "tracking_number": body.tracking_number,
               "status": "pending"}
    claims = api(TABLE, "POST", params={"on_conflict": "organization_id,order_request_id,fulfillment_order_id"},
        data={"organization_id": str(oid), "order_request_id": str(rid),
              "fulfillment_order_id": group["id"], **payload}, prefer="resolution=ignore-duplicates,return=representation")
    if not claims:
        claims = api(TABLE, "PATCH", params={**key, "status": "eq.failed"}, data=payload,
            prefer="return=representation")
    if not claims:
        raise HTTPException(409, "A shipment is already recorded or awaiting confirmation. Refresh its status.")
    try:
        result = graphql(domain, token, CREATE, {"input": {
            "lineItemsByFulfillmentOrder": [{"fulfillmentOrderId": group["id"],
                "fulfillmentOrderLineItems": [{"id": item["id"], "quantity": item["remainingQuantity"]}
                                               for item in items]}],
            "trackingInfo": {"company": body.carrier, "number": body.tracking_number},
            "notifyCustomer": False,
        }}).get("fulfillmentCreate") or {}
    except HTTPException as exc:
        raise HTTPException(502, "Shopify did not confirm the shipment. Check the pending shipment before retrying.") from exc
    fulfillment = result.get("fulfillment") or {}
    if not fulfillment and result.get("userErrors"):
        update_job(key, attempt, {"status": "failed"})
        raise HTTPException(422, "Shopify declined the shipment. Check the items, location and tracking details.")
    fulfillment_id = fulfillment.get("id")
    numeric_id(fulfillment_id, "Fulfillment")
    update_job(key, attempt, {"status": "recorded", "fulfillment_id": fulfillment_id})
    return {"status": "recorded"}


@router.post("/reconcile")
def reconcile_shipment(oid: UUID, rid: UUID, body: Recheck, user=Depends(current_user)):
    membership(str(oid), user, MANAGERS)
    domain, token, order, _ = provider_order(oid, rid)
    key = shipment_key(oid, rid, body.fulfillment_order_id)
    rows = api(TABLE, params={**key, "select": "status,attempt_id,tracking_number,carrier", "limit": 1})
    if not rows:
        raise HTTPException(404, "Shipment request not found.")
    job = rows[0]
    if job["status"] != "pending":
        return {"status": job["status"]}
    group = graphql(domain, token, TRACKING, {"id": body.fulfillment_order_id}).get("fulfillmentOrder")
    if not group or group["orderId"] != order["id"]:
        raise HTTPException(409, "Shopify did not return this shipment group.")
    connection = group["fulfillments"]
    if connection["pageInfo"]["hasNextPage"]:
        raise HTTPException(409, "Review the shipment history in Shopify before trying again.")
    matches = [item for item in connection["nodes"] if item["status"] != "CANCELLED"
        and any(str(tracking.get("number") or "").casefold() == job["tracking_number"].casefold()
                and str(tracking.get("company") or "").casefold() == job["carrier"].casefold()
                for tracking in item["trackingInfo"])]
    if len(matches) > 1:
        raise HTTPException(409, "More than one Shopify shipment matches this tracking number.")
    if not matches:
        return {"status": "pending", "message": "Shopify has not confirmed this shipment yet."}
    fulfillment_id = matches[0]["id"]
    numeric_id(fulfillment_id, "Fulfillment")
    update_job(key, job["attempt_id"], {"status": "recorded", "fulfillment_id": fulfillment_id})
    return {"status": "recorded"}
