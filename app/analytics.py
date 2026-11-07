"""Workspace call reporting, independent of the paged session inbox."""

from datetime import date, datetime, timedelta, timezone
from uuid import UUID
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app.account import current_user, membership, rpc
from app.cloud import api, org_or_404

router = APIRouter(prefix="/api/account/organizations/{oid}", tags=["analytics"])
MANAGERS = ("owner", "admin")


class MetricInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    label: str = Field(min_length=2, max_length=50)
    stage: Literal["hot", "warm", "general", "not_qualified", "needs_review"] | None = None
    direction: Literal["inbound", "outbound", "other"] | None = None
    min_seconds: int | None = Field(default=None, ge=1, le=14400)
    open_follow_up: bool = False


@router.get("/analytics")
def analytics(oid: UUID, days: int = Query(30, ge=1, le=365),
              start: date | None = None, end: date | None = None,
              user=Depends(current_user)):
    workspace = str(oid)
    role = membership(workspace, user)
    if start is None and end is None:
        org = org_or_404(workspace)
        try:
            zone = ZoneInfo(org.get("timezone") or "UTC")
        except (ZoneInfoNotFoundError, ValueError):
            zone = timezone.utc
        end = datetime.now(zone).date()
        start = end - timedelta(days=days - 1)
    if start is None or end is None or not 0 <= (end - start).days < 366:
        raise HTTPException(422, "Choose an ordered date range of one year or less.")
    if not 1900 <= start.year <= end.year <= 2200:
        raise HTTPException(422, "Choose dates between 1900 and 2200.")
    result = rpc("workspace_call_analytics", {
        "p_workspace": workspace, "p_start": start.isoformat(), "p_end": end.isoformat(),
    })
    if not isinstance(result, dict):
        raise HTTPException(502, "Analytics could not be loaded.")
    return {"start": start.isoformat(), "end": end.isoformat(), "can_manage": role in MANAGERS, **result}


@router.post("/analytics/metrics")
def add_metric(oid: UUID, body: MetricInput, user=Depends(current_user)):
    workspace = str(oid)
    membership(workspace, user, MANAGERS)
    if not (body.stage or body.direction or body.min_seconds or body.open_follow_up):
        raise HTTPException(422, "Choose at least one condition for this metric.")
    result = rpc("add_workspace_call_metric", {
        "p_workspace": workspace, "p_label": body.label, "p_stage": body.stage,
        "p_direction": body.direction, "p_min_seconds": body.min_seconds, "p_open_follow_up": body.open_follow_up,
    })
    if result == "duplicate":
        raise HTTPException(409, "A metric with this name already exists.")
    if result == "limit":
        raise HTTPException(409, "This workspace has eight saved metrics. Remove one to add another.")
    if result != "created":
        raise HTTPException(502, "The metric could not be saved.")
    return {"created": True}


@router.delete("/analytics/metrics/{metric_id}")
def remove_metric(oid: UUID, metric_id: UUID, user=Depends(current_user)):
    workspace = str(oid)
    membership(workspace, user, MANAGERS)
    api("workspace_call_metrics", "DELETE", params={
        "organization_id": "eq." + workspace, "id": "eq." + str(metric_id),
    }, prefer="return=minimal")
    return {"removed": True}
