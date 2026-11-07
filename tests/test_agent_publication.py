"""Publish state, versioned snapshots and rollback for the managed receptionist."""
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import agent_builder
from app.account import current_user
from app.main import app

client = TestClient(app)
WORKSPACE = "00000000-0000-0000-0000-00000000000a"
CONFIG = {
    "agent_name": "Alex", "greeting": "Hi, this is Alex, an AI assistant for Metro.",
    "call_instructions": "Be brief.", "stock_voice_id": "v1", "speech_language": "en",
    "speech_keyterms": [], "speech_pronunciations": [], "voice_leads_enabled": True,
    "voice_booking_enabled": False, "voice_catalog_enabled": False,
    "voice_order_requests_enabled": False, "transfer_directory": [], "call_menu": {},
}


class Fake:
    def __init__(self):
        self.config = dict(CONFIG)
        self.publications = []
        self.synced = 0
        self.sync_error = None

    def api(self, table, method="GET", params=None, **kwargs):
        if table == "agent_configs":
            return [dict(self.config)]
        rows = sorted(self.publications, key=lambda row: -row["version"])
        if params and "id" in params:
            rows = [row for row in rows if row["id"] == params["id"][3:]]
        return rows[: params.get("limit", 50)] if params else rows

    def rpc(self, name, payload):
        assert name == "record_agent_publication"
        current = (self.config["agent_name"], self.config["greeting"], self.config["call_instructions"])
        if current != (payload["p_agent_name"], payload["p_greeting"], payload["p_call_instructions"]):
            return None
        version = len(self.publications) + 1
        self.publications.append({
            "id": "00000000-0000-0000-0000-0000000001%02d" % version, "version": version,
            "agent_name": payload["p_agent_name"], "greeting": payload["p_greeting"],
            "call_instructions": payload["p_call_instructions"], "settings": payload["p_settings"],
            "source": payload["p_source"], "note": payload["p_note"], "created_at": "2026-11-07T00:00:00Z",
        })
        return version

    def sync(self, workspace):
        if self.sync_error:
            raise HTTPException(409, self.sync_error)
        self.synced += 1

    def save(self, workspace, edit):
        self.config.update(agent_name=edit.agent_name, greeting=edit.greeting,
                           call_instructions=edit.call_instructions)


@pytest.fixture
def fake(monkeypatch):
    state = Fake()
    monkeypatch.setattr(agent_builder, "api", state.api)
    monkeypatch.setattr(agent_builder, "rpc", state.rpc)
    monkeypatch.setattr(agent_builder, "sync_assistant", state.sync)
    monkeypatch.setattr(agent_builder, "save_agent_configuration", state.save)
    monkeypatch.setattr(agent_builder, "membership", lambda *a, **k: {"role": "owner"})
    app.dependency_overrides[current_user] = lambda: {"id": "00000000-0000-0000-0000-0000000000f1"}
    yield state
    app.dependency_overrides.clear()


URL = "/api/account/organizations/%s/agent" % WORKSPACE


def test_never_published_workspace_reports_unpublished_changes(fake):
    body = client.get(URL + "/publication").json()
    assert body == {"unpublished_changes": True, "current_version": None, "versions": []}


def test_publish_syncs_then_records_a_snapshot_and_clears_the_unpublished_flag(fake):
    result = client.post(URL + "/publish", json={"note": "  First launch  "}).json()
    assert result == {"version": 1, "unchanged": False}
    assert fake.synced == 1 and fake.publications[0]["note"] == "First launch"
    assert fake.publications[0]["settings"]["voice_leads_enabled"] is True
    status = client.get(URL + "/publication").json()
    assert status["unpublished_changes"] is False and status["current_version"] == 1


def test_editing_after_publish_marks_unpublished_and_republishing_without_changes_is_idempotent(fake):
    client.post(URL + "/publish")
    assert client.post(URL + "/publish").json() == {"version": 1, "unchanged": True}
    assert len(fake.publications) == 1
    fake.config["greeting"] = "Hello, Alex here, the AI assistant for Metro."
    assert client.get(URL + "/publication").json()["unpublished_changes"] is True
    fake.config["voice_booking_enabled"] = True
    assert client.post(URL + "/publish").json()["version"] == 2


def test_failed_sync_records_no_publication(fake):
    fake.sync_error = "The connected assistant cannot be updated here."
    response = client.post(URL + "/publish")
    assert response.status_code == 409 and fake.publications == []


def test_publish_conflicts_when_configuration_changes_during_sync(fake, monkeypatch):
    def edit_during_sync(workspace):
        fake.config["greeting"] = "Edited while syncing, an AI assistant."
    monkeypatch.setattr(agent_builder, "sync_assistant", edit_during_sync)
    assert client.post(URL + "/publish").status_code == 409
    assert fake.publications == []


def test_rollback_restores_identity_syncs_and_records_a_new_version(fake):
    client.post(URL + "/publish")
    original = dict(fake.config)
    fake.config.update(agent_name="Sam", greeting="Sam here, AI assistant for Metro.")
    client.post(URL + "/publish")
    first = fake.publications[0]["id"]
    result = client.post(URL + "/publication/%s/rollback" % first).json()
    assert result["version"] == 3 and result["restored_version"] == 1
    assert fake.config["agent_name"] == original["agent_name"]
    assert fake.publications[2]["source"] == "rollback"
    assert fake.publications[2]["note"] == "Rolled back to version 1"


def test_rollback_failure_to_sync_keeps_draft_but_records_nothing(fake):
    client.post(URL + "/publish")
    fake.config["agent_name"] = "Sam"
    first = fake.publications[0]["id"]
    fake.sync_error = "The connected assistant cannot be updated here."
    response = client.post(URL + "/publication/%s/rollback" % first)
    assert response.status_code == 409
    assert "restored as the draft" in response.json()["detail"]
    assert fake.config["agent_name"] == "Alex" and len(fake.publications) == 1


def test_rollback_unknown_version_is_not_found(fake):
    response = client.post(URL + "/publication/00000000-0000-0000-0000-00000000dead/rollback")
    assert response.status_code == 404


def test_viewers_cannot_publish_or_roll_back(fake, monkeypatch):
    def deny(*args, **kwargs):
        raise HTTPException(403, "Insufficient role")
    monkeypatch.setattr(agent_builder, "membership", deny)
    assert client.post(URL + "/publish").status_code == 403
    assert client.get(URL + "/publication").status_code == 403
    assert client.post(URL + "/publication/00000000-0000-0000-0000-00000000dead/rollback").status_code == 403
    assert fake.publications == []
