from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from app import account, team
from app.main import app

ORG = "123e4567-e89b-12d3-a456-426614174000"
USER = "123e4567-e89b-12d3-a456-426614174001"
PATH = f"/api/account/organizations/{ORG}/invitations"


@pytest.fixture
def signed_in(monkeypatch):
    app.dependency_overrides[account.current_user] = lambda: {"id": USER, "email": "owner@example.com"}
    monkeypatch.setattr(team, "membership", lambda *args: "owner")
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(account.current_user, None)


def test_invite_uses_email_bound_link_and_never_stores_raw_token(signed_in, monkeypatch):
    captured = {}

    def api(table, method, **kwargs):
        assert (table, method) == ("workspace_invitations", "POST")
        captured.update(kwargs)
        data = kwargs["data"]
        return [{"id": USER, "email": data["email"], "role": data["role"], "expires_at": data["expires_at"]}]

    monkeypatch.setattr(team, "api", api)
    response = signed_in.post(PATH, json={"email": "Viewer@Example.com", "role": "viewer"})
    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    link = urlsplit(response.json()["link"])
    assert link.path == "/app"
    assert link.fragment.startswith("invite=")
    token = link.fragment.removeprefix("invite=")
    assert captured["data"]["token_hash"] == team.digest(token)
    assert captured["data"]["email"] == "viewer@example.com"
    assert captured["params"]["on_conflict"] == "organization_id,email,role"
    assert "token_hash" not in response.json()


def test_admin_can_only_invite_or_revoke_viewers(signed_in, monkeypatch):
    monkeypatch.setattr(team, "membership", lambda *args: "admin")
    writes = []
    monkeypatch.setattr(team, "api", lambda table, method, **kwargs: writes.append(kwargs))
    assert signed_in.post(PATH, json={"email": "test@example.com", "role": "admin"}).status_code == 403
    assert not writes
    assert signed_in.delete(PATH + "/" + USER).status_code == 204
    assert writes[0]["params"]["organization_id"] == f"eq.{ORG}"
    assert writes[0]["params"]["role"] == "eq.viewer"


def test_accept_passes_verified_session_identity_to_atomic_rpc(signed_in, monkeypatch):
    token = "a" * 43

    def accept(name, body):
        assert name == "accept_workspace_invitation"
        assert body == {"p_hash": team.digest(token), "p_email": "owner@example.com", "p_user_id": USER}
        return ORG

    monkeypatch.setattr(team, "rpc", accept)
    path = "/api/account/invitations/accept"
    assert signed_in.post(path, json={"token": token}).json() == {"organization_id": ORG}
    monkeypatch.setattr(team, "rpc", lambda *args: None)
    assert signed_in.post(path, json={"token": token}).status_code == 404
