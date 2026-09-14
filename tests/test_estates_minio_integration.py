"""Opt-in real S3-compatible verification for private Estate binaries."""
from __future__ import annotations

import os

import pytest
from fastapi import HTTPException

from app.services.estates import documents
from app.utils.r2_objects import build_r2_settings, create_r2_client


ENDPOINT = str(os.getenv("LANDCHECK_MINIO_TEST_ENDPOINT") or "").strip()
pytestmark = pytest.mark.storage


def _configure(monkeypatch: pytest.MonkeyPatch) -> dict:
    if not ENDPOINT:
        pytest.skip("LANDCHECK_MINIO_TEST_ENDPOINT is not configured")
    bucket = str(os.getenv("LANDCHECK_MINIO_TEST_BUCKET") or "landcheck-estates-test")
    monkeypatch.setenv("R2_ENDPOINT_URL", ENDPOINT)
    monkeypatch.setenv("R2_BUCKET", bucket)
    monkeypatch.setenv("R2_ACCESS_KEY_ID", str(os.getenv("LANDCHECK_MINIO_TEST_ACCESS_KEY") or ""))
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", str(os.getenv("LANDCHECK_MINIO_TEST_SECRET_KEY") or ""))
    monkeypatch.setenv("R2_REGION", "us-east-1")
    settings = build_r2_settings(prefix="R2")
    if not settings:
        raise RuntimeError("MinIO integration settings are incomplete")
    client = create_r2_client(settings)
    existing = {item["Name"] for item in client.list_buckets().get("Buckets", [])}
    if bucket not in existing:
        client.create_bucket(Bucket=bucket)
    return settings


def test_private_estate_storage_round_trip_and_validation(monkeypatch: pytest.MonkeyPatch):
    settings = _configure(monkeypatch)
    client = create_r2_client(settings)
    valid = [
        ("receipt.pdf", "application/pdf", b"%PDF-1.7 integration"),
        ("receipt.jpg", "image/jpeg", b"\xff\xd8\xff\xe0 integration"),
        ("receipt.png", "image/png", b"\x89PNG\r\n\x1a\n integration"),
    ]
    for filename, mime, payload in valid:
        stored = documents.store_private_estate_file(organization_uid="org-a", category="payments", entity_uid="payment-a", filename=filename, content_type=mime, data=payload)
        assert stored.object_key.startswith("estates/org_org-a/payments/payment-a/")
        assert stored.size_bytes == len(payload)
        assert "http" not in stored.object_key
        head = client.head_object(Bucket=settings["bucket"], Key=stored.object_key)
        assert head["ContentType"] == mime
        data, retrieved_mime = documents.read_private_estate_file(stored.object_key)
        assert data == payload
        assert retrieved_mime == mime
    for filename, mime, payload in [("fake.exe", "application/pdf", b"%PDF-1.7"), ("fake.pdf", "image/png", b"%PDF-1.7"), ("empty.pdf", "application/pdf", b""), ("bad.pdf", "application/pdf", b"not a PDF")]:
        with pytest.raises(HTTPException):
            documents.store_private_estate_file(organization_uid="org-a", category="documents", entity_uid="document-a", filename=filename, content_type=mime, data=payload)
    monkeypatch.setattr(documents, "MAX_ESTATE_DOCUMENT_BYTES", 5)
    with pytest.raises(HTTPException) as error:
        documents.store_private_estate_file(organization_uid="org-a", category="documents", entity_uid="document-a", filename="large.pdf", content_type="application/pdf", data=b"%PDF-large")
    assert error.value.status_code == 413
