"""Account deletion is confirmed, guarded and removes the sign-in only after data cleanup."""
import pytest
from fastapi.testclient import TestClient

from app import account_delete as mod
from app.account import current_user
from app.main import app

client = TestClient(app)
W = "00000000-0000-0000-0000-00000000000a"


class Db:
    def __init__(self, others=False, status=None, phone=None, role="owner"):
        self.others, self.status, self.phone, self.role = others, status, phone, role
        self.deleted, self.auth_deleted = [], []

    def api(self, table, method="GET", params=None, **kw):
        if table == "organization_members" and method == "GET":
            if "organization_id" in params:
                return [{"user_id": "other"}] if self.others else []
            return [{"organization_id": W, "role": self.role}]
        if table == "agent_configs":
            return [{"provider_phone_id": self.phone}]
        if method == "DELETE":
            self.deleted.append(table)
        return []


@pytest.fixture
def setup(monkeypatch):
    def make(**kw):
        db = Db(**kw)
        monkeypatch.setattr(mod, "api", db.api)
        monkeypatch.setattr(mod, "billing_account", lambda w: {"status": db.status} if db.status else None)
        monkeypatch.setattr(mod, "delete_auth_user", lambda uid: db.auth_deleted.append(uid))
        return db
    app.dependency_overrides[current_user] = lambda: {"id": "u1"}
    yield make
    app.dependency_overrides.clear()


def call(confirm="DELETE"):
    return client.request("DELETE", "/api/account/me", json={"confirm": confirm})


def test_requires_typed_confirmation(setup):
    db = setup()
    assert call("delete").status_code == 422 and db.deleted == [] and db.auth_deleted == []


@pytest.mark.parametrize("kwargs,text", [
    ({"others": True}, "team members"), ({"status": "active"}, "subscription"), ({"phone": "ph_1"}, "phone number")])
def test_blocks_when_it_would_strand_people_money_or_a_number(setup, kwargs, text):
    db = setup(**kwargs)
    response = call()
    assert response.status_code == 409 and text in response.json()["detail"]
    assert db.deleted == [] and db.auth_deleted == []


def test_deletes_owned_workspace_then_membership_then_sign_in(setup):
    db = setup()
    assert call().json() == {"deleted": True}
    assert db.deleted == ["organizations", "organization_members"] and db.auth_deleted == ["u1"]


def test_members_who_own_nothing_only_lose_their_membership(setup):
    db = setup(role="viewer")
    assert call().status_code == 200
    assert db.deleted == ["organization_members"] and db.auth_deleted == ["u1"]
