import io

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app import account
from app.main import app
from app.pdf_import import extract_pdf

ORG = "123e4567-e89b-12d3-a456-426614174000"
CHECK = "123e4567-e89b-12d3-a456-426614174001"
BASE = f"/api/account/organizations/{ORG}"
TEXT = "Our front desk accepts walk-ins every Tuesday."


def sample_pdf(*, encrypted=False, blank=False):
    writer = PdfWriter()
    page = writer.add_blank_page(612, 792)
    if not blank:
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({
                NameObject("/F1"): DictionaryObject({
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                })
            })
        })
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 72 700 Td ({TEXT}) Tj ET".encode())
        page.replace_contents(stream)
    if encrypted:
        writer.encrypt("test")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


@pytest.fixture
def signed_in(monkeypatch):
    app.dependency_overrides[account.current_user] = lambda: {"id": "qa"}
    monkeypatch.setattr(account, "membership", lambda oid, user, roles=None: "owner")
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(account.current_user, None)


def test_pdf_extracts_real_text():
    assert extract_pdf(sample_pdf()) == TEXT


@pytest.mark.parametrize("options, error", [
    ({"blank": True}, "selectable text"),
    ({"encrypted": True}, "unlocked PDF"),
])
def test_pdf_rejects_unusable_files(options, error):
    with pytest.raises(HTTPException, match=error):
        extract_pdf(sample_pdf(**options))


def test_pdf_upload_saves_draft_with_workspace_deduplication(signed_in, monkeypatch):
    calls = []

    def database(table, method, **kwargs):
        calls.append((table, method, kwargs))
        return [{"id": CHECK}] if len(calls) == 1 else []

    monkeypatch.setattr(account, "api", database)
    args = {
        "params": {"filename": "hours.pdf"},
        "headers": {"Content-Type": "application/pdf"},
        "content": sample_pdf(),
    }
    url = BASE + "/descriptions/import-pdf"
    assert signed_in.post(url, **args).json() == {"created": True}
    assert signed_in.post(url, **args).json() == {"created": False}
    table, method, options = calls[0]
    assert (table, method) == ("knowledge_documents", "POST")
    assert options["params"] == {"on_conflict": "organization_id,source_sha256"}
    assert options["data"]["organization_id"] == ORG
    assert options["data"]["source_filename"] == "hours.pdf"
    assert options["data"]["content"] == TEXT
    assert "approved_at" not in options["data"]
    assert len(options["data"]["source_sha256"]) == 64


def test_pdf_limit_is_applied_before_parse(signed_in, monkeypatch):
    monkeypatch.setattr(account, "MAX_PDF_BYTES", 8)
    response = signed_in.post(BASE + "/descriptions/import-pdf", params={"filename": "x.pdf"},
                              content=b"%PDF-too-large", headers={"Content-Type": "application/pdf"})
    assert response.status_code == 413


def test_viewer_cannot_create_pdf_or_checks(signed_in, monkeypatch):
    def viewer(oid, user, roles=None):
        if roles is not None:
            raise HTTPException(404, "Workspace not found")
        return "viewer"

    monkeypatch.setattr(account, "membership", viewer)
    assert signed_in.post(BASE + "/descriptions/import-pdf?filename=test.pdf").status_code == 404
    assert signed_in.post(BASE + "/knowledge-checks", json={
        "question": "Do you take walk-ins?", "expectation": "unknown",
    }).status_code == 404


def test_knowledge_check_pass_failure_and_workspace_filter(signed_in, monkeypatch):
    check = {"id": CHECK, "question": "Do you take walk-ins?",
             "expectation": "contains", "expected_text": "EVERY Tuesday"}

    def database(table, *, params):
        assert table == "knowledge_checks"
        assert params["id"] == f"eq.{CHECK}"
        return [check] if params["organization_id"] == f"eq.{ORG}" else []

    monkeypatch.setattr(account, "api", database)
    monkeypatch.setattr(account, "answer_question", lambda oid, question: {
        "answer": "We accept walk-ins every\nTuesday.", "source": "approved_semantic",
    })
    url = f"/knowledge-checks/{CHECK}/run"
    assert signed_in.post(BASE + url).json()["passed"] is True
    check["expected_text"] = "Wednesday"
    assert signed_in.post(BASE + url).json()["passed"] is False
    assert signed_in.post("/api/account/organizations/" + CHECK + url).status_code == 404


def test_knowledge_check_requires_expected_phrase(signed_in):
    assert signed_in.post(BASE + "/knowledge-checks", json={
        "question": "Do you take walk-ins?", "expectation": "contains", "expected_text": " ",
    }).status_code == 422


def test_published_plans_do_not_replace_discount_refusal():
    result = account.answer_question(account.CALLPILOT_ORG, "Can I get a discount on your plan?")
    assert result["source"] == "policy"
