"""Phase 5 GeoJSON response correctness checks for the isolated synthetic dataset."""

from __future__ import annotations

import json
import os

import pytest
from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request


DATABASE_URL = os.getenv("PHASE5_DATABASE_URL")
if not DATABASE_URL:
    pytest.skip("set PHASE5_DATABASE_URL to run Phase 5 integration checks", allow_module_level=True)
if not DATABASE_URL.startswith("postgresql"):
    pytest.skip("Phase 5 checks require the isolated PostgreSQL fixture", allow_module_level=True)

from app.models.estate_foundation import Estate, EstatePlot  # noqa: E402
from app.routers import estates as estate_router  # noqa: E402


@pytest.fixture(scope="module")
def db_session():
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/estates/3/plots.geojson",
            "raw_path": b"/estates/3/plots.geojson",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 50000),
            "server": ("127.0.0.1", 18000),
        }
    )


def _legacy_payload(db, estate_id: int) -> dict:
    rows = db.query(
        EstatePlot.id,
        EstatePlot.plot_number,
        EstatePlot.commercial_status,
        EstatePlot.development_status,
        EstatePlot.geometry_status,
        EstatePlot.area_sqm,
        EstatePlot.public_address,
        EstatePlot.asking_price,
        EstatePlot.block_id,
        EstatePlot.title_verified,
        EstatePlot.title_verified_note,
        func.ST_AsGeoJSON(EstatePlot.geometry, 7).label("geometry_json"),
    ).filter(EstatePlot.estate_id == estate_id, EstatePlot.geometry.isnot(None)).all()
    features = []
    for row in rows:
        geometry = json.loads(row.geometry_json) if isinstance(row.geometry_json, str) else row.geometry_json
        if geometry is None:
            continue
        features.append(
            {
                "type": "Feature",
                "id": row.id,
                "properties": {
                    "id": row.id,
                    "plot_number": row.plot_number,
                    "commercial_status": row.commercial_status,
                    "development_status": row.development_status,
                    "geometry_status": row.geometry_status,
                    "area_sqm": float(row.area_sqm or 0),
                    "public_address": row.public_address,
                    "asking_price": str(row.asking_price) if row.asking_price is not None else None,
                    "block_id": row.block_id,
                    "title_verified": bool(row.title_verified),
                    "title_verified_note": row.title_verified_note,
                },
                "geometry": geometry,
            }
        )
    return {"type": "FeatureCollection", "features": features}


def test_geojson_matches_previous_semantics_for_all_synthetic_estate_sizes(db_session, monkeypatch):
    monkeypatch.setattr(estate_router, "require_estate_view_access", lambda *_args, **_kwargs: None)
    for estate_id, expected_count in ((1, 100), (2, 1000), (3, 5000)):
        estate = db_session.get(Estate, estate_id)
        assert estate is not None
        expected = _legacy_payload(db_session, estate_id)
        response = estate_router.estate_plots_geojson(estate_id, _request(), db_session)
        actual = json.loads(response.body)
        assert response.media_type == "application/json"
        assert actual == expected
        assert len(actual["features"]) == expected_count
