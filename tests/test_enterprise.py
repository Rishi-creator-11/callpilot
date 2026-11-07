"""Enterprise inquiries are validated, rate limited and bot-resistant."""
import pytest
from fastapi.testclient import TestClient

from app import enterprise
from app.main import app

client = TestClient(app)
GOOD = {"name": "Ana Lopez", "email": "ana@example.com", "company": "Metro Group", "message": "Five locations"}


@pytest.fixture(autouse=True)
def store(monkeypatch):
    enterprise._hits.clear()
    rows = []
    monkeypatch.setattr(enterprise, "api", lambda table, method="GET", data=None, **k: rows.append((table, data)) or [])
    return rows


def test_valid_inquiry_is_stored(store):
    assert client.post("/api/enterprise-inquiry", json=GOOD).status_code == 201
    assert store[0][0] == "enterprise_inquiries" and store[0][1]["company"] == "Metro Group" and store[0][1]["phone"] is None


def test_invalid_email_and_short_names_are_rejected(store):
    assert client.post("/api/enterprise-inquiry", json={**GOOD, "email": "nope"}).status_code == 422
    assert client.post("/api/enterprise-inquiry", json={**GOOD, "name": "A"}).status_code == 422
    assert store == []


def test_honeypot_pretends_success_and_stores_nothing(store):
    assert client.post("/api/enterprise-inquiry", json={**GOOD, "website": "http://spam"}).status_code == 201
    assert store == []


def test_rate_limit_per_client(store):
    codes = [client.post("/api/enterprise-inquiry", json=GOOD, headers={"x-forwarded-for": "9.9.9.9"}).status_code for _ in range(4)]
    assert codes == [201, 201, 201, 429] and len(store) == 3


def test_forged_leftmost_forwarded_address_does_not_reset_the_limit(store):
    codes = [client.post("/api/enterprise-inquiry", json=GOOD,
                         headers={"x-forwarded-for": "1.1.1.%d, 8.8.8.8" % i}).status_code for i in range(4)]
    assert codes == [201, 201, 201, 429]


def test_page_renders_form():
    page = client.get("/enterprise")
    assert page.status_code == 200 and "enterprise-form" in page.text
