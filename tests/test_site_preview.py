"""Workspace-scoped full-site import preview tests.

The preview must never save, approve, or publish extracted business data.
"""
import asyncio

from fastapi import HTTPException
import pytest

from app import account, site_reader


def test_preview_website_reads_full_site_without_saving(monkeypatch):
    checked = []
    monkeypatch.setattr(account, "membership", lambda oid, user, roles: checked.append((oid, user, roles)))
    monkeypatch.setattr(account, "api", lambda *args, **kwargs: pytest.fail("Preview must not write or read Supabase"))
    payload = {
        "source_url": "https://example.com/",
        "title": "Demo Shop",
        "description": "Prints and design",
        "stats": {"pages_read": 12, "products": 2, "seconds": 0.5},
        "products": [{"name": "Business Cards", "from_price": "$25", "details": "Matte"}],
        "pages": [{"url": "https://example.com/", "kind": "home"}],
        "phone": "", "email": "", "hours": [], "policies": [], "faqs": [],
    }
    monkeypatch.setattr(site_reader, "read_site", lambda url: payload)
    user = {"id": "test-owner"}
    result = asyncio.run(account.preview_website("test-org", account.WebsiteImport(url="https://example.com"), user=user))
    assert checked == [("test-org", user, ("owner", "admin"))]
    assert result["products"][0]["from_price"] == "$25"
    assert result["requires_review"] is True
    assert result["saved"] is False


def test_preview_denies_nonmembers_before_crawl(monkeypatch):
    def deny(*args):
        raise HTTPException(404, "Workspace not found")
    monkeypatch.setattr(account, "membership", deny)
    monkeypatch.setattr(site_reader, "read_site", lambda _: pytest.fail("Unauthorized crawl"))
    with pytest.raises(HTTPException) as error:
        asyncio.run(account.preview_website("other-org", account.WebsiteImport(url="https://example.com"), user={"id": "no-access"}))
    assert error.value.status_code == 404
