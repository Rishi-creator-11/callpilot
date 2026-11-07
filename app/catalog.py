"""Owner-approved native catalog and pending order requests.

No checkout, stock decrement, payment or fulfillment happens here. An order
request is not an accepted order. All queries are scoped to an authenticated
organization and use server-side credentials.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from app.account import current_user, membership
from app.cloud import api

router = APIRouter(prefix="/api/account/organizations/{oid}", tags=["catalog"])


class CatalogItemInput(BaseModel):
    sku: str = Field(min_length=2, max_length=60, pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
    name: str = Field(min_length=2, max_length=160)
    description: str = Field(default="", max_length=1200)
    unit_price_cents: int | None = Field(default=None, ge=0, le=1000000000)
    stock_quantity: int | None = Field(default=None, ge=0, le=100000000)


def _item(oid: str, iid: uuid.UUID) -> dict:
    rows = api("catalog_items", params={
        "organization_id": "eq." + oid, "id": "eq." + str(iid),
        "select": "id,sku,name,description,unit_price_cents,stock_quantity,active,approved_at",
        "limit": 1,
    })
    if not rows:
        raise HTTPException(404, "Catalog item not found")
    return rows[0]


@router.get("/catalog")
def list_catalog(oid: str, user=Depends(current_user)):
    membership(oid, user)
    return api("catalog_items", params={
        "organization_id": "eq." + oid,
        "select": "id,sku,name,description,unit_price_cents,stock_quantity,active,approved_at,updated_at",
        "order": "name.asc", "limit": 200,
    })


@router.post("/catalog")
def create_catalog_item(oid: str, body: CatalogItemInput, user=Depends(current_user)):
    membership(oid, user, ("owner","admin"))
    clean = body.model_dump()
    clean.update({"sku": clean["sku"].strip().upper(), "name":clean["name"].strip(),
                  "description":clean["description"].strip()})
    rows = api("catalog_items", "POST", data={
        "organization_id": oid, **clean, "active":False,
    }, prefer="return=representation")
    return {"id":rows[0]["id"],"approved":False,
            "message":"Catalog item saved as a draft. Approve it before callers can see it."}


@router.put("/catalog/{iid}")
def edit_catalog_item(oid: str, iid: uuid.UUID, body: CatalogItemInput,
                      user=Depends(current_user)):
    membership(oid, user, ("owner","admin"))
    _item(oid, iid)
    clean=body.model_dump()
    clean.update({"sku":clean["sku"].strip().upper(),"name":clean["name"].strip(),
                  "description":clean["description"].strip(),
                  "active":False,"approved_at":None,"approved_by":None,
                  "updated_at":datetime.now(timezone.utc).isoformat()})
    api("catalog_items", "PATCH", params={"organization_id":"eq."+oid,
        "id":"eq."+str(iid)},data=clean,prefer="return=minimal")
    return {"saved":True,"active":False,"requires_reapproval":True}


@router.post("/catalog/{iid}/approve")
def approve_catalog_item(oid: str, iid: uuid.UUID,user=Depends(current_user)):
    membership(oid, user, ("owner","admin"))
    _item(oid, iid)
    now=datetime.now(timezone.utc).isoformat()
    api("catalog_items", "PATCH", params={"organization_id":"eq."+oid,
        "id":"eq."+str(iid)},data={"active":True,"approved_at":now,
           "approved_by":user["id"],"updated_at":now},prefer="return=minimal")
    return {"approved":True,"active":True}


@router.post("/catalog/{iid}/archive")
def archive_catalog_item(oid: str, iid: uuid.UUID,user=Depends(current_user)):
    membership(oid, user, ("owner","admin"))
    _item(oid, iid)
    api("catalog_items", "PATCH", params={"organization_id":"eq."+oid,
        "id":"eq."+str(iid)},data={"active":False,"approved_at":None,
         "approved_by":None},prefer="return=minimal")
    return {"archived":True}


@router.get("/orders/requests")
def order_requests(oid: str,user=Depends(current_user)):
    membership(oid, user)
    return api("order_requests",params={"organization_id":"eq."+oid,
        "select":"id,sku,quantity,caller_name,caller_contact,status,created_at",
        "order":"created_at.desc","limit":200})
