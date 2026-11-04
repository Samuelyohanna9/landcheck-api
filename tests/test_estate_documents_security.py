from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.models.estate_foundation import EstateDocumentLink
from app.routers.estate_construction import DRAWING_TYPES
from app.routers.estates import _make_estate_document_link
from app.services.estates import documents


class FakeR2:
    def __init__(self): self.objects = {}
    def put_object(self, **kwargs): self.objects[kwargs["Key"]] = kwargs


@pytest.fixture()
def r2(monkeypatch):
    client = FakeR2()
    monkeypatch.setattr(documents, "build_r2_settings", lambda **_: {"bucket": "test"})
    monkeypatch.setattr(documents, "create_r2_client", lambda _: client)
    return client

@pytest.mark.parametrize(("filename", "mime", "payload"), [
    ("receipt.pdf", "application/pdf", b"%PDF-1.7 evidence"),
    ("receipt.jpg", "image/jpeg", b"\xff\xd8\xff\xe0 evidence"),
    ("receipt.png", "image/png", b"\x89PNG\r\n\x1a\n evidence"),
])
def test_private_store_accepts_valid_signatures(r2, filename, mime, payload):
    stored = documents.store_private_estate_file(organization_uid="org", category="payments", entity_uid="payment", filename=filename, content_type=mime, data=payload)
    assert stored.object_key.startswith("estates/org_org/payments/payment/")
    assert stored.object_key in r2.objects

@pytest.mark.parametrize(("filename", "mime", "payload"), [
    ("receipt.exe", "application/pdf", b"%PDF-1.7"),
    ("receipt.pdf", "image/png", b"%PDF-1.7"),
    ("receipt.pdf", "application/pdf", b"not-a-pdf"),
    ("receipt.pdf", "application/pdf", b""),
])
def test_private_store_rejects_invalid_uploads(r2, filename, mime, payload):
    with pytest.raises(HTTPException):
        documents.store_private_estate_file(organization_uid="org", category="documents", entity_uid="document", filename=filename, content_type=mime, data=payload)

def test_private_store_rejects_oversize_upload(r2, monkeypatch):
    monkeypatch.setattr(documents, "MAX_ESTATE_DOCUMENT_BYTES", 5)
    with pytest.raises(HTTPException) as error:
        documents.store_private_estate_file(organization_uid="org", category="documents", entity_uid="document", filename="a.pdf", content_type="application/pdf", data=b"%PDF-too-large")
    assert error.value.status_code == 413


def test_estate_document_links_include_the_rls_tenant_key():
    link = _make_estate_document_link(organization_id=42, document_id=7, entity_type="plot", entity_id=19)

    assert isinstance(link, EstateDocumentLink)
    assert link.organization_id == 42
    assert link.document_id == 7
    assert link.entity_id == "19"
    assert EstateDocumentLink.__table__.c.organization_id.nullable is False


def test_construction_site_plans_use_the_same_approval_workflow_as_other_drawings():
    assert "site_plan" in DRAWING_TYPES
