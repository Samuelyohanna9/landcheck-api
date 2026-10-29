"""Local prototypes for element-aware Canva flyer workflows.

This module deliberately has no network side effects and is not wired into the
existing Canva routes yet. It keeps the exact request shapes for a later,
credentialed integration test separate from the current flattened-image path.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from typing import Any


PDF_IMPORT_SCOPE = "design:content:write"
AUTOFILL_SCOPES = (
    "design:content:read",
    "design:content:write",
    "design:meta:read",
    "brandtemplate:meta:read",
    "brandtemplate:content:read",
    "asset:read",
    "asset:write",
)


class CanvaPrototypeDependencyError(RuntimeError):
    """Raised when the optional local PDF inspection dependency is unavailable."""


def _title_base64(title: str) -> str:
    value = str(title or "LandCheck flyer").strip()[:50] or "LandCheck flyer"
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def pdf_import_headers(title: str) -> dict[str, str]:
    """Build the documented headers for Canva's PDF design-import endpoint."""

    metadata = {
        "title_base64": _title_base64(title),
        "mime_type": "application/pdf",
    }
    return {
        "Content-Type": "application/octet-stream",
        "Import-Metadata": json.dumps(metadata, separators=(",", ":")),
    }


def autofill_data(
    *,
    estate_name: str,
    location: str,
    price_text: str,
    size_text: str,
    headline: str,
    description: str,
    contact_name: str,
    contact_phone: str,
    contact_email: str,
    hero_asset_id: str | None = None,
    logo_asset_id: str | None = None,
    map_asset_id: str | None = None,
) -> dict[str, dict[str, str]]:
    """Map LandCheck flyer fields to a representative tagged Canva template.

    The keys are intentionally stable, readable field names that should be
    created in the Canva template using Bulk Create/Labels. Canva media fields
    require already-uploaded Canva asset IDs, so missing media is omitted.
    """

    data: dict[str, dict[str, str]] = {
        "ESTATE_NAME": {"type": "text", "text": str(estate_name)},
        "LOCATION": {"type": "text", "text": str(location)},
        "PRICE": {"type": "text", "text": str(price_text)},
        "SIZE": {"type": "text", "text": str(size_text)},
        "HEADLINE": {"type": "text", "text": str(headline)},
        "DESCRIPTION": {"type": "text", "text": str(description)},
        "CONTACT_NAME": {"type": "text", "text": str(contact_name)},
        "CONTACT_PHONE": {"type": "text", "text": str(contact_phone)},
        "CONTACT_EMAIL": {"type": "text", "text": str(contact_email)},
    }
    for field, asset_id in (
        ("HERO_IMAGE", hero_asset_id),
        ("LOGO", logo_asset_id),
        ("MAP_IMAGE", map_asset_id),
    ):
        if asset_id:
            data[field] = {"type": "image", "asset_id": str(asset_id)}
    return data


def autofill_payload(
    brand_template_id: str,
    data: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Build a documented ``create_from_brand_template`` Autofill payload."""

    template_id = str(brand_template_id or "").strip()
    if not template_id:
        raise ValueError("brand_template_id is required")
    if not data:
        raise ValueError("at least one Autofill field is required")
    return {
        "type": "create_from_brand_template",
        "brand_template_id": template_id,
        "data": {str(key): dict(value) for key, value in data.items()},
    }


def profile_pdf(pdf_bytes: bytes) -> dict[str, int | bool]:
    """Inspect a generated PDF for selectable text, images, and vector drawings.

    This is evidence about LandCheck's source PDF, not proof that Canva will
    preserve every object as an editable Canva element.
    """

    try:
        import pymupdf as fitz
    except ImportError as exc:  # pragma: no cover - depends on local environment
        raise CanvaPrototypeDependencyError(
            "PyMuPDF is required for local PDF profiling; install requirements.txt first."
        ) from exc

    with fitz.open(stream=bytes(pdf_bytes), filetype="pdf") as document:
        pages = list(document)
        text_chars = sum(len(page.get_text()) for page in pages)
        text_blocks = sum(
            sum(1 for block in page.get_text("blocks") if len(block) >= 5 and str(block[4]).strip())
            for page in pages
        )
        image_blocks = sum(
            sum(1 for block in page.get_text("dict").get("blocks", []) if block.get("type") == 1)
            for page in pages
        )
        image_xobjects = sum(len(page.get_images(full=True)) for page in pages)
        vector_drawings = sum(len(page.get_drawings()) for page in pages)
        return {
            "pages": len(pages),
            "text_chars": text_chars,
            "text_blocks": text_blocks,
            "image_blocks": image_blocks,
            "image_xobjects": image_xobjects,
            "vector_drawings": vector_drawings,
            "has_selectable_text": text_chars > 0,
            "has_images": image_blocks > 0 or image_xobjects > 0,
            "has_vector_drawings": vector_drawings > 0,
        }
