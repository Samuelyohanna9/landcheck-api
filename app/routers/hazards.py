from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
import os
import tempfile
import logging

from app.db import SessionLocal
from app.utils.hazard_common import classify_risk, risk_tier_legend
from app.utils.hazard_flood import compute_flood_risk, overlay_to_data_url as flood_overlay_to_data_url
from app.utils.hazard_erosion import compute_erosion_risk, overlay_to_data_url as erosion_overlay_to_data_url
from app.utils.hazard_pdf import render_flood_report_pdf, render_erosion_report_pdf
from app.utils.r2_exports import upload_export_file_best_effort


router = APIRouter(prefix="/hazards", tags=["hazards"])
logger = logging.getLogger("hazards")

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
REPORTS_DIR = os.path.join(BASE_DIR, "reports")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

RASTER_LEGEND_FLOOD = [
    {"label": "Flood Depth (Raster) - Low", "color": "#e0f2fe"},
    {"label": "Flood Depth (Raster) - Moderate", "color": "#0ea5e9"},
    {"label": "Flood Depth (Raster) - High", "color": "#1d4ed8"},
]
RASTER_LEGEND_EROSION = [
    {"label": "Slope (Raster) - Gentle", "color": "#fef9c3"},
    {"label": "Slope (Raster) - Moderate", "color": "#f97316"},
    {"label": "Slope (Raster) - Steep", "color": "#7c2d12"},
]


def _pdf_response_with_r2(local_pdf_path: str, filename: str, category: str):
    upload_meta = upload_export_file_best_effort(
        local_pdf_path,
        filename,
        category=category,
        content_type="application/pdf",
    )
    response = FileResponse(local_pdf_path, media_type="application/pdf", filename=filename)
    if upload_meta:
        object_key = upload_meta.get("object_key")
        public_url = upload_meta.get("public_url")
        if object_key:
            response.headers["X-LandCheck-R2-Key"] = str(object_key)
        if public_url:
            response.headers["X-LandCheck-R2-Url"] = str(public_url)
    return response


def _extract_boundary(payload: dict) -> dict:
    if payload.get("type") and payload.get("coordinates"):
        return payload
    if payload.get("geometry"):
        return payload["geometry"]
    if payload.get("boundary"):
        return payload["boundary"]
    return payload


def _extract_local_elevation_points(payload: dict) -> list:
    points = payload.get("local_elevation_points") or payload.get("elevation_points") or []
    if not isinstance(points, list):
        return []
    valid = []
    for p in points:
        if not isinstance(p, dict):
            continue
        try:
            valid.append({
                "lng": float(p["lng"]),
                "lat": float(p["lat"]),
                "elevation_m": float(p["elevation_m"]),
            })
        except (KeyError, TypeError, ValueError):
            continue
    return valid


def _build_legend(risk_class: str, class_color: str, plot_label: str, show_raster: bool, raster_legend: list) -> list:
    legend = [{"label": f"{plot_label} - {risk_class}", "color": class_color}]
    legend.extend(risk_tier_legend())
    if show_raster:
        legend.extend(raster_legend)
    return legend


# ---------------- FLOOD ----------------

@router.post("/flood/preview")
def flood_preview(payload: dict = Body(...), db: Session = Depends(get_db)):
    boundary = _extract_boundary(payload)
    show_raster = bool(payload.get("show_raster", False))
    return_period = int(payload.get("return_period", 100))
    local_elevation_points = _extract_local_elevation_points(payload)
    try:
        risk_value, risk_class, breakdown, overlay_png = compute_flood_risk(
            db, boundary, show_raster, return_period, local_elevation_points
        )
    except Exception as exc:
        logger.exception("Flood preview failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    _, class_color = classify_risk(risk_value, breakdown.get("data_available", True))
    legend = _build_legend(risk_class, class_color, "Plot Class (Polygon)", show_raster, RASTER_LEGEND_FLOOD)

    note = "Screening-level flood risk based on global datasets."
    if not breakdown.get("data_available", True):
        note = (
            "No flood depth data available for this location at the selected return period. "
            "Try a different return period or enable local raster to inspect coverage."
        )

    response = {
        "risk_score": round(risk_value * 100, 1),
        "risk_class": risk_class,
        "class_color": class_color,
        "mean_depth_m": round(breakdown["mean_depth_m"], 2),
        "max_depth_m": round(breakdown["max_depth_m"], 2),
        "inundation_percent": round(breakdown["inundation_fraction"] * 100, 1),
        "distance_to_river_m": round(breakdown["distance_to_river_m"], 1),
        "depth_score": round(breakdown["depth_score"], 3),
        "inundation_score": round(breakdown["inundation_score"], 3),
        "river_proximity_score": round(breakdown["river_proximity_score"], 3),
        "buildings_total": breakdown.get("buildings_total", 0),
        "buildings_threatened": breakdown.get("buildings_threatened", 0),
        "overlay": flood_overlay_to_data_url(overlay_png),
        "note": note,
        "buffer_m": 1000,
        "method": (
            "Risk score uses GloFAS flood depth for the selected return period. "
            "We compute mean depth and inundation fraction inside the plot, "
            "adjust with distance to major rivers, normalize depth (0-3m), "
            "then combine: 60% depth + 25% inundation + 15% river proximity. "
            "Higher score indicates higher river flood susceptibility."
        ),
        "return_period": return_period,
        "data_available": bool(breakdown.get("data_available", True)),
        "legend": legend,
        "show_raster": show_raster,
        "local_elevation_used": bool(breakdown.get("local_elevation_used")),
        "relative_elevation_m": breakdown.get("relative_elevation_m"),
        "local_mean_elevation_m": breakdown.get("local_mean_elevation_m"),
        "regional_mean_elevation_m": breakdown.get("regional_mean_elevation_m"),
    }
    return response


@router.post("/flood/pdf")
def flood_pdf(payload: dict = Body(...), db: Session = Depends(get_db)):
    boundary = _extract_boundary(payload)
    show_raster = bool(payload.get("show_raster", False))
    return_period = int(payload.get("return_period", 100))
    local_elevation_points = _extract_local_elevation_points(payload)
    try:
        risk_value, risk_class, breakdown, overlay_png = compute_flood_risk(
            db, boundary, show_raster, return_period, local_elevation_points
        )
    except Exception as exc:
        logger.exception("Flood PDF failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    _, class_color = classify_risk(risk_value, breakdown.get("data_available", True))
    legend = _build_legend(risk_class, class_color, "Plot Class (Polygon)", show_raster, RASTER_LEGEND_FLOOD)

    os.makedirs(REPORTS_DIR, exist_ok=True)
    tmp_pdf = tempfile.NamedTemporaryFile(suffix="_flood_report.pdf", delete=False)
    pdf_path = tmp_pdf.name
    tmp_pdf.close()

    note = "Screening-level flood risk based on global datasets."
    if not breakdown.get("data_available", True):
        note = (
            "No flood depth data available for this location at the selected return period. "
            "Try a different return period."
        )

    summary = {
        "risk_score": f"{round(risk_value * 100, 1)}",
        "risk_class": risk_class,
        "class_color": class_color,
        "mean_depth_m": f"{round(breakdown['mean_depth_m'], 2)}",
        "max_depth_m": f"{round(breakdown['max_depth_m'], 2)}",
        "inundation_percent": f"{round(breakdown['inundation_fraction'] * 100, 1)}",
        "distance_to_river_m": f"{round(breakdown['distance_to_river_m'], 1)}",
        "depth_score": breakdown["depth_score"],
        "inundation_score": breakdown["inundation_score"],
        "river_proximity_score": breakdown["river_proximity_score"],
        "buildings_total": breakdown.get("buildings_total", 0),
        "buildings_threatened": breakdown.get("buildings_threatened", 0),
        "buffer_m": "1000",
        "note": note,
        "legend": legend,
        "return_period": str(return_period),
        "show_raster": str(show_raster),
        "local_elevation_used": bool(breakdown.get("local_elevation_used")),
        "relative_elevation_m": breakdown.get("relative_elevation_m"),
    }
    render_flood_report_pdf(pdf_path, overlay_png, summary)
    return _pdf_response_with_r2(pdf_path, "flood_risk_report.pdf", "hazard-flood")


# ---------------- EROSION ----------------

@router.post("/erosion/preview")
def erosion_preview(payload: dict = Body(...)):
    boundary = _extract_boundary(payload)
    show_raster = bool(payload.get("show_raster", False))
    local_elevation_points = _extract_local_elevation_points(payload)
    try:
        risk_value, risk_class, breakdown, overlay_png = compute_erosion_risk(
            boundary, show_raster, local_elevation_points
        )
    except Exception as exc:
        logger.exception("Erosion preview failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    _, class_color = classify_risk(risk_value, breakdown.get("data_available", True))
    legend = _build_legend(risk_class, class_color, "Plot Class (Polygon)", show_raster, RASTER_LEGEND_EROSION)

    note = "Screening-level erosion susceptibility based on global terrain and satellite data."
    if not breakdown.get("data_available", True):
        note = "No elevation data available for this location. Try a different site or contact support."

    response = {
        "risk_score": round(risk_value * 100, 1),
        "risk_class": risk_class,
        "class_color": class_color,
        "mean_slope_deg": round(breakdown["mean_slope_deg"], 2),
        "max_slope_deg": round(breakdown["max_slope_deg"], 2),
        "mean_ndvi": round(breakdown["mean_ndvi"], 3),
        "distance_to_drainage_m": round(breakdown["distance_to_drainage_m"], 1),
        "slope_score": round(breakdown["slope_score"], 3),
        "vegetation_score": round(breakdown["vegetation_score"], 3),
        "drainage_score": round(breakdown["drainage_score"], 3),
        "overlay": erosion_overlay_to_data_url(overlay_png),
        "note": note,
        "buffer_m": 500,
        "method": (
            "Susceptibility index (not a full RUSLE soil-loss estimate). We sample slope from a "
            "global 30m DEM, vegetation cover (NDVI) from recent cloud-free Sentinel-2 imagery, "
            "and distance to the nearest natural drainage channel from HydroSHEDS flow "
            "accumulation, then combine: 50% slope + 30% bare-ground exposure + 20% drainage "
            "concentration. Higher score indicates higher erosion susceptibility."
        ),
        "data_available": bool(breakdown.get("data_available", True)),
        "legend": legend,
        "show_raster": show_raster,
        "slope_source": breakdown.get("slope_source", "unavailable"),
    }
    return response


@router.post("/erosion/pdf")
def erosion_pdf(payload: dict = Body(...)):
    boundary = _extract_boundary(payload)
    show_raster = bool(payload.get("show_raster", False))
    local_elevation_points = _extract_local_elevation_points(payload)
    try:
        risk_value, risk_class, breakdown, overlay_png = compute_erosion_risk(
            boundary, show_raster, local_elevation_points
        )
    except Exception as exc:
        logger.exception("Erosion PDF failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    _, class_color = classify_risk(risk_value, breakdown.get("data_available", True))
    legend = _build_legend(risk_class, class_color, "Plot Class (Polygon)", show_raster, RASTER_LEGEND_EROSION)

    os.makedirs(REPORTS_DIR, exist_ok=True)
    tmp_pdf = tempfile.NamedTemporaryFile(suffix="_erosion_report.pdf", delete=False)
    pdf_path = tmp_pdf.name
    tmp_pdf.close()

    note = "Screening-level erosion susceptibility based on global terrain and satellite data."
    if not breakdown.get("data_available", True):
        note = "No elevation data available for this location."

    summary = {
        "risk_score": f"{round(risk_value * 100, 1)}",
        "risk_class": risk_class,
        "class_color": class_color,
        "mean_slope_deg": f"{round(breakdown['mean_slope_deg'], 2)}",
        "max_slope_deg": f"{round(breakdown['max_slope_deg'], 2)}",
        "mean_ndvi": f"{round(breakdown['mean_ndvi'], 3)}",
        "distance_to_drainage_m": f"{round(breakdown['distance_to_drainage_m'], 1)}",
        "slope_score": breakdown["slope_score"],
        "vegetation_score": breakdown["vegetation_score"],
        "drainage_score": breakdown["drainage_score"],
        "note": note,
        "legend": legend,
        "show_raster": str(show_raster),
        "slope_source": breakdown.get("slope_source", "unavailable"),
    }
    render_erosion_report_pdf(pdf_path, overlay_png, summary)
    return _pdf_response_with_r2(pdf_path, "erosion_risk_report.pdf", "hazard-erosion")
