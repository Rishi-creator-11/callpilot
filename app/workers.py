"""Ordered call playbooks for a workspace's managed receptionist."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.account import current_user, membership, rpc
from app.cloud import api

router = APIRouter(prefix="/api/account/organizations/{oid}/workers")


class Playbook(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=2, max_length=80)
    when: str = Field(min_length=5, max_length=240)
    instructions: str = Field(min_length=5, max_length=600)
    enabled: bool = True


class PlaybookUpdate(BaseModel):
    previous: list[Playbook] = Field(max_length=12)
    playbooks: list[Playbook] = Field(max_length=12)


@router.get("")
def get_workers(oid: UUID, user=Depends(current_user)):
    workspace = str(oid)
    role = membership(workspace, user)
    rows = api("agent_configs", params={
        "organization_id": "eq." + workspace, "select": "worker_playbooks", "limit": 1,
    })
    return {"playbooks": rows[0]["worker_playbooks"] if rows else [], "can_edit": role in ("owner", "admin")}


@router.put("")
def save_workers(oid: UUID, body: PlaybookUpdate, user=Depends(current_user)):
    workspace = str(oid)
    membership(workspace, user, ("owner", "admin"))
    names = {entry.name.casefold() for entry in body.playbooks}
    if len(names) != len(body.playbooks):
        raise HTTPException(422, "Give each worker a different name.")
    before = [entry.model_dump() for entry in body.previous]
    after = [entry.model_dump() for entry in body.playbooks]
    saved = rpc("save_worker_playbooks", {
        "p_workspace": workspace, "p_previous": before, "p_playbooks": after,
    })
    if saved is not True:
        raise HTTPException(409, "The saved workers changed. Reopen Workers and review the latest configuration.")
    return {"playbooks": after}
