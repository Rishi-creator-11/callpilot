"""Workspace call reporting, independent of the paged session inbox."""

from datetime import date, datetime, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query

from app.account import current_user, membership, rpc
from app.cloud import org_or_404

router = APIRouter(prefix="/api/account/organizations/{oid}", tags=["analytics"])


@router.get("/analytics")
def analytics(oid: UUID, days: int = Query(30, ge=1, le=365),
              start: date | None = None, end: date | None = None,
              user=Depends(current_user)):
    workspace = str(oid)
    membership(workspace, user)
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
    return {"start": start.isoformat(), "end": end.isoformat(), **result}
