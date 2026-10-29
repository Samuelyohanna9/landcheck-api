"""Run a local, offline Canva editability prototype.

The script generates synthetic LandCheck-style PDFs through the existing
renderer, profiles their source objects, and prints the exact Canva PDF-import
and Autofill request shapes. It never calls Canva and never writes to
production or a database.
"""

from __future__ import annotations

import json
import sys
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image, ImageDraw
from geoalchemy2.shape import from_shape
from shapely.geometry import Polygon

from app.services.estates.canva_editability import (
    AUTOFILL_SCOPES,
    autofill_data,
    autofill_payload,
    pdf_import_headers,
    profile_pdf,
)
from app.services.estates.marketing_pdf import render_flyer_pdf


def _sample_image() -> bytes:
    image = Image.new("RGB", (1200, 600), (80, 130, 100))
    draw = ImageDraw.Draw(image)
    draw.rectangle((40, 40, 1160, 560), outline=(255, 255, 255), width=8)
    draw.text((80, 260), "LandCheck prototype image", fill=(255, 255, 255))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _sample_logo() -> bytes:
    image = Image.new("RGBA", (420, 140), (26, 143, 90, 255))
    draw = ImageDraw.Draw(image)
    draw.text((24, 48), "LANDCHECK", fill=(255, 255, 255, 255))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _sample_context(image: bytes, logo: bytes):
    boundary = from_shape(Polygon([(7.40, 9.00), (7.41, 9.00), (7.41, 9.01), (7.40, 9.01), (7.40, 9.00)]), srid=4326)
    plot = SimpleNamespace(
        id=1,
        plot_number="A-001",
        geometry=from_shape(Polygon([(7.401, 9.001), (7.404, 9.001), (7.404, 9.004), (7.401, 9.004), (7.401, 9.001)]), srid=4326),
        geometry_status="approved",
        commercial_status="available",
        asking_price=25000000,
        area_sqm=500,
        public_address="North access road",
    )
    estate = SimpleNamespace(
        name="Amana Estate",
        public_show_prices=True,
        public_description="A secure serviced estate near Abuja.",
        public_tagline="Build with confidence.",
        approximate_area_sqm=120000,
        public_development_forecast=None,
        boundary=boundary,
    )
    return SimpleNamespace(
        estate=estate,
        organization_name="Sample Realty",
        logo_bytes=logo,
        plots=[plot],
        features=[],
        available_plots=[plot],
        payment_plan=[],
        counts={},
        available=12,
        reserved=2,
        sold=3,
        show_prices=True,
        page_url="https://example.test/estate/amana/",
        stamp="29 Oct 2026",
        agent_name="Sample Realty",
        contact_phone="08000000000",
        whatsapp_digits="2348000000000",
        contact_email="sales@example.test",
        city="Abuja",
        location="Abuja, Nigeria",
        min_price=None,
        available_plots_count=12,
    )


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    image = _sample_image()
    # Avoid a network tile request: this is a source-PDF structure probe, not
    # a map-rendering or Canva connectivity test.
    context = _sample_context(image, _sample_logo())
    style_profiles = {}
    with patch("app.services.estates.marketing_design.fetch_satellite", return_value=None):
        for style in ("luxury", "promo", "heritage", "bold", "blueprint"):
            try:
                style_pdf = render_flyer_pdf(context, style)
                style_profiles[style] = {"bytes": len(style_pdf), **profile_pdf(style_pdf)}
            except Exception as exc:
                style_profiles[style] = {"error": f"{type(exc).__name__}: {exc}"}
        pdf = render_flyer_pdf(context, "luxury")
    data = autofill_data(
        estate_name="Amana Estate",
        location="Abuja, Nigeria",
        price_text="₦25,000,000",
        size_text="500 sqm",
        headline="A considered place to build",
        description="Serviced plots with a clear public layout.",
        contact_name="Sample Realty",
        contact_phone="08000000000",
        contact_email="sales@example.test",
        hero_asset_id="asset-hero-example",
        logo_asset_id="asset-logo-example",
        map_asset_id="asset-map-example",
    )
    result = {
        "pdf_profile": {"bytes": len(pdf), **profile_pdf(pdf)},
        "style_profiles": style_profiles,
        "pdf_import": {
            "endpoint": "POST https://api.canva.com/rest/v1/imports",
            "required_scope": "design:content:write",
            "headers": pdf_import_headers("Amana Estate · LandCheck flyer"),
            "body": "generated PDF bytes (not sent)",
        },
        "autofill": {
            "endpoint": "POST https://api.canva.com/rest/v1/autofills",
            "required_scopes": list(AUTOFILL_SCOPES),
            "payload": autofill_payload("brand-template-example", data),
            "body": "request prepared locally (not sent)",
        },
        "live_canva_editability_verified": False,
        "live_canva_editability_reason": "No isolated Canva test account was used by this offline probe.",
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
