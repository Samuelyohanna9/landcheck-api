from __future__ import annotations

import base64
import json

import pytest

from app.models.estate_canva import EstateCanvaConnection
from app.services.estates.canva_editability import (
    AUTOFILL_SCOPES,
    CanvaPrototypeDependencyError,
    autofill_data,
    autofill_payload,
    pdf_import_headers,
    profile_pdf,
)
from app.services.estates import canva_connect


@pytest.fixture()
def canva_env(monkeypatch):
    monkeypatch.setenv("CANVA_ENABLED", "true")
    monkeypatch.setenv("CANVA_CLIENT_ID", "client-id")
    monkeypatch.setenv("CANVA_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("CANVA_SECRET_KEY", "local-test-secret")


def test_pdf_import_headers_use_documented_metadata_shape():
    headers = pdf_import_headers("Amana Estate · Flyer 🚀")
    metadata = json.loads(headers["Import-Metadata"])

    assert headers["Content-Type"] == "application/octet-stream"
    assert metadata["mime_type"] == "application/pdf"
    assert base64.b64decode(metadata["title_base64"]).decode("utf-8") == "Amana Estate · Flyer 🚀"


def test_autofill_data_contains_editable_text_and_uploaded_media_fields():
    data = autofill_data(
        estate_name="Amana Estate",
        location="Abuja",
        price_text="₦25,000,000",
        size_text="500 sqm",
        headline="A place to build",
        description="Serviced plots.",
        contact_name="Sample Realty",
        contact_phone="08000000000",
        contact_email="sales@example.test",
        hero_asset_id="hero-1",
        logo_asset_id="logo-1",
        map_asset_id="map-1",
    )

    assert data["ESTATE_NAME"] == {"type": "text", "text": "Amana Estate"}
    assert data["PRICE"]["type"] == "text"
    assert data["HERO_IMAGE"] == {"type": "image", "asset_id": "hero-1"}
    assert data["MAP_IMAGE"] == {"type": "image", "asset_id": "map-1"}


def test_autofill_payload_is_brand_template_request():
    payload = autofill_payload("brand-123", {"ESTATE_NAME": {"type": "text", "text": "Amana"}})

    assert payload == {
        "type": "create_from_brand_template",
        "brand_template_id": "brand-123",
        "data": {"ESTATE_NAME": {"type": "text", "text": "Amana"}},
    }
    assert "brandtemplate:content:read" in AUTOFILL_SCOPES
    assert "asset:write" in AUTOFILL_SCOPES


def test_autofill_payload_rejects_missing_template_or_data():
    with pytest.raises(ValueError):
        autofill_payload("", {"FIELD": {"type": "text", "text": "value"}})
    with pytest.raises(ValueError):
        autofill_payload("brand-123", {})


def test_pdf_profile_reports_selectable_text_for_a_small_pdf():
    reportlab = pytest.importorskip("reportlab.pdfgen.canvas")
    from io import BytesIO

    output = BytesIO()
    pdf = reportlab.Canvas(output)
    pdf.drawString(40, 780, "Amana Estate")
    pdf.save()

    try:
        profile = profile_pdf(output.getvalue())
    except CanvaPrototypeDependencyError:
        pytest.skip("PyMuPDF is not installed in this local test environment")

    assert profile["has_selectable_text"] is True
    assert profile["text_chars"] >= len("Amana Estate")


def test_pdf_import_uses_binary_body_and_returns_one_design(canva_env, monkeypatch):
    calls = []

    def fake_request_json(_db, _connection, method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/imports":
            return {"job": {"id": "import-1"}}
        return {"job": {"status": "success", "result": {"designs": [{"id": "design-1", "urls": {"edit_url": "https://www.canva.com/api/design/edit"}}]}}}

    monkeypatch.setattr(canva_connect, "request_json", fake_request_json)
    design = canva_connect.import_pdf(object(), EstateCanvaConnection(status="active"), b"%PDF-test", "Amana Estate 🚀")

    assert design["id"] == "design-1"
    assert calls[0][0:2] == ("POST", "/imports")
    assert calls[0][2]["body"] == b"%PDF-test"
    metadata = json.loads(calls[0][2]["headers"]["Import-Metadata"])
    assert metadata["mime_type"] == "application/pdf"


def test_autofill_design_posts_payload_and_returns_design(canva_env, monkeypatch):
    def fake_request_json(_db, _connection, method, path, **kwargs):
        if path == "/autofills":
            assert method == "POST"
            assert kwargs["json_body"]["type"] == "create_from_brand_template"
            return {"job": {"id": "autofill-1"}}
        return {"job": {"status": "success", "result": {"design": {"id": "design-2", "urls": {"edit_url": "https://www.canva.com/api/design/edit"}}}}}

    monkeypatch.setattr(canva_connect, "request_json", fake_request_json)
    design = canva_connect.autofill_design(object(), EstateCanvaConnection(status="active"), {"type": "create_from_brand_template", "brand_template_id": "brand-1", "data": {"NAME": {"type": "text", "text": "Amana"}}})

    assert design["id"] == "design-2"


def test_autofill_configuration_requires_template_and_all_scopes(canva_env, monkeypatch):
    monkeypatch.setenv("CANVA_AUTOFILL_BRAND_TEMPLATE_ID", "brand-1")
    connection = EstateCanvaConnection(status="active", scopes=" ".join(sorted(set(canva_connect.DEFAULT_SCOPES.split()) | set(canva_connect.AUTOFILL_SCOPES))))

    assert canva_connect.autofill_configured() is True
    assert canva_connect.connection_has_scopes(connection, set(canva_connect.AUTOFILL_SCOPES)) is True
