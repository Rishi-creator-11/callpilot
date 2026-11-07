"""Human transfer destinations for a CallPilot-managed phone assistant."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.account import current_user, membership, rpc
from app.cloud import api

router = APIRouter(prefix="/api/account/organizations/{oid}/transfer-directory", tags=["channels"])
ROUTE_PREFIX = "[CallPilot route] "
MANAGERS = ("owner", "admin")


class Destination(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    label: str = Field(min_length=2, max_length=60)
    number: str = Field(pattern=r"^\+[1-9][0-9]{7,14}$")
    when: str = Field(min_length=5, max_length=240)
    enabled: bool = True


class DirectoryEdit(BaseModel):
    previous: list[Destination] = Field(max_length=8)
    destinations: list[Destination] = Field(max_length=8)


def reject_loops(workspace: str, destinations: list[dict]):
    if not destinations:
        return
    linked = api("phone_assignments", params={
        "organization_id": "eq." + workspace, "select": "provider_number",
    })
    own_numbers = {row.get("provider_number") for row in linked}
    if any(item["number"] in own_numbers for item in destinations):
        raise HTTPException(422, "A transfer destination cannot be the receptionist’s own phone number.")


def native_transfer_tool(workspace: str, directory: list[dict]) -> dict | None:
    destinations = [item for item in directory if item.get("enabled")]
    if not destinations:
        return None
    reject_loops(workspace, destinations)
    return {
        "type": "transferCall",
        "destinations": [
            {"type": "number", "number": item["number"],
             "description": ROUTE_PREFIX + item["label"] + ": " + item["when"]}
            for item in destinations
        ],
    }


def is_managed_transfer(tool: object) -> bool:
    if not isinstance(tool, dict) or tool.get("type") != "transferCall":
        return False
    destinations = tool.get("destinations")
    return isinstance(destinations, list) and bool(destinations) and all(
        isinstance(item, dict) and str(item.get("description") or "").startswith(ROUTE_PREFIX)
        for item in destinations
    )


@router.get("")
def get_directory(oid: UUID, user=Depends(current_user)):
    workspace = str(oid)
    role = membership(workspace, user)
    if role not in MANAGERS:
        return {"destinations": [], "can_manage": False}
    rows = api("agent_configs", params={
        "organization_id": "eq." + workspace, "select": "transfer_directory", "limit": 1,
    })
    return {"destinations": rows[0]["transfer_directory"] if rows else [], "can_manage": True}


@router.put("")
def save_directory(oid: UUID, body: DirectoryEdit, user=Depends(current_user)):
    workspace = str(oid)
    membership(workspace, user, MANAGERS)
    if len({item.label.casefold() for item in body.destinations}) != len(body.destinations):
        raise HTTPException(422, "Use a different label for each transfer destination.")
    numbers = {item.number for item in body.destinations}
    if len(numbers) != len(body.destinations):
        raise HTTPException(422, "List each number only once.")
    if any(number.startswith("+1900") or (number.startswith("+1") and number[5:8] == "976") for number in numbers):
        raise HTTPException(422, "Premium-rate numbers cannot be used for transfers.")
    destinations = [item.model_dump() for item in body.destinations]
    reject_loops(workspace, destinations)
    if rpc("save_workspace_transfer_directory", {
        "p_workspace": workspace, "p_previous": [item.model_dump() for item in body.previous],
        "p_directory": destinations,
    }) is not True:
        raise HTTPException(409, "The saved directory changed. Refresh and review it again.")
    return {"destinations": destinations}
