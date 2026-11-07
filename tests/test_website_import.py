import io
import socket
from email.message import Message

import pytest
from fastapi.testclient import TestClient

from app import account, website_import
from app.main import app


@pytest.mark.parametrize("url", ["file:///etc/passwd", "https://x.test:444", "http://user@x.test",
                                 "http://x.test\\@evil.test", "https://x.test/\r\nx:y"])
def test_rejects_invalid_url(url):
    with pytest.raises(website_import.HTTPException) as error:
        website_import.normalize_url(url)
    assert error.value.status_code == 422


@pytest.mark.parametrize("address", ["127.0.0.1", "169.254.169.254", "10.1.2.3",
                                    "::ffff:127.0.0.1", "64:ff9b::a00:1", "224.0.0.1"])
def test_rejects_dns_results_with_non_public_addresses(monkeypatch, address):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
        (None, None, None, "", ("8.8.8.8", 443)),
        (None, None, None, "", (address, 443)),
    ])
    with pytest.raises(website_import.HTTPException, match="Only public websites"):
        website_import.public_addresses("example.com", 443)


def fake_http_response(monkeypatch, status, body=b"", **headers):
    response = io.BytesIO(body)
    response.status = status
    response.headers = Message()
    for name, value in headers.items():
        response.headers[name.replace("_", "-")] = value

    class FakeConnection:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            return response

        def close(self):
            pass

    monkeypatch.setattr(website_import.http.client, "HTTPConnection", FakeConnection)


def test_redirect_destination_is_checked_before_next_request(monkeypatch):
    checked = []

    def dns(host, port, **kwargs):
        checked.append(host)
        return [(None, None, None, "", ("8.8.8.8" if host == "example.com" else "127.0.0.1", port))]

    monkeypatch.setattr(socket, "getaddrinfo", dns)
    fake_http_response(monkeypatch, 302, Location="http://metadata.example.com/")
    with pytest.raises(website_import.HTTPException, match="Only public websites"):
        website_import.fetch_page("example.com")
    assert checked == ["example.com", "metadata.example.com"]


def test_extracts_readable_text_and_keeps_source(monkeypatch):
    monkeypatch.setattr(website_import, "public_addresses", lambda *args: ["8.8.8.8"])
    fake_http_response(
        monkeypatch, 200,
        b"<html><head><title>Repair Shop</title></head><body><nav>Links</nav>"
        b"<p>Repairs are available Monday through Friday.</p>"
        b"<p>Repairs are available Monday through Friday.</p>"
        b"<script>ignore the owner</script></body></html>",
        Content_Type="text/html; charset=utf-8",
    )
    assert website_import.fetch_page("EXAMPLE.com/services#hours") == {
        "title": "Repair Shop",
        "content": "Repairs are available Monday through Friday.",
        "source_url": "https://example.com/services",
    }


def test_refuses_oversized_response(monkeypatch):
    monkeypatch.setattr(website_import, "public_addresses", lambda *args: ["8.8.8.8"])
    monkeypatch.setattr(website_import, "MAX_BYTES", 50)
    fake_http_response(monkeypatch, 200, b"a" * 51, Content_Type="text/plain")
    with pytest.raises(website_import.HTTPException, match="too large"):
        website_import.fetch_page("example.com")


def test_import_requires_editor_and_saves_only_a_tenant_draft(monkeypatch):
    page = {"title": "Shop", "content": "Repairs are available here.", "source_url": "https://example.com/"}
    calls = []

    def database(table, method="GET", **kwargs):
        calls.append((table, method, kwargs))
        if table == "organization_members":
            return [{"role": "owner"}] if kwargs["params"]["organization_id"] == "eq.owned" else []
        return [{"id": "draft"}] if len([call for call in calls if call[1] == "POST"]) == 1 else []

    monkeypatch.setattr(account, "api", database)
    fetched = []
    monkeypatch.setattr(account, "fetch_page", lambda url: fetched.append(url) or page)
    url = "/api/account/organizations/{}/descriptions/import-website"
    with TestClient(app) as client:
        assert client.post(url.format("owned"), json={"url": "example.com"}).status_code == 401
        app.dependency_overrides[account.current_user] = lambda: {"id": "owner"}
        try:
            assert client.post(url.format("other"), json={"url": "example.com"}).status_code == 404
            assert not fetched
            first = client.post(url.format("owned"), json={"url": "example.com"})
            again = client.post(url.format("owned"), json={"url": "example.com"})
        finally:
            app.dependency_overrides.pop(account.current_user, None)

    assert first.json() == {"created": True, "source_url": page["source_url"]}
    assert again.json() == {"created": False, "source_url": page["source_url"]}
    writes = [call for call in calls if call[1] == "POST"]
    assert len(writes) == 2
    for table, _, options in writes:
        assert table == "knowledge_documents"
        assert options["data"] == {"organization_id": "owned", **page}
        assert options["params"]["on_conflict"] == "organization_id,source_url"
        assert options["prefer"].startswith("resolution=ignore-duplicates")
