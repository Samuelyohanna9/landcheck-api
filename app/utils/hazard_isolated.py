from __future__ import annotations

"""Child-process entry point for the Estate hazard screening (see isolated_run.py)."""


def screen_boundary(boundary: dict) -> dict:
    from app.db import SessionLocal
    from app.routers.hazards import erosion_preview, flood_preview

    payload = {"boundary": boundary, "show_raster": False}
    db = SessionLocal()
    try:
        return {"flood": flood_preview(payload, db), "erosion": erosion_preview(payload, db)}
    finally:
        db.close()


def screen_boundary_soil(boundary: dict) -> dict:
    """Same child-process pattern as screen_boundary, for the standalone Soil Analysis tool (kept
    separate from Hazard Analysis - see soil_analysis.py's module docstring for why)."""
    from app.db import SessionLocal
    from app.routers.estates import _soil_preview_payload
    from app.utils.soil_analysis import compute_soil_analysis

    db = SessionLocal()
    try:
        risk_value, risk_class, breakdown, _overlay_png = compute_soil_analysis(db, boundary)
        return {"soil": _soil_preview_payload(risk_value, risk_class, breakdown)}
    finally:
        db.close()
