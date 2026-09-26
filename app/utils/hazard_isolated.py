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
