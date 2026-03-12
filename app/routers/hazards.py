from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import FileResponse
import os
import tempfile
import logging

from app.utils.hazard_flood import compute_flood_risk, overlay_to_data_url
from app.utils.hazard_pdf import render_flood_report_pdf
from app.utils.r2_exports import upload_export_file_best_effort


router = APIRouter(prefix="/hazards", tags=["hazards"])
logger = logging.getLogger("hazards")

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
REPORTS_DIR = os.path.join(BASE_DIR, "reports")


def _pdf_response_with_r2(local_pdf_path: str, filename: str):
    upload_meta = upload_export_file_best_effort(
        local_pdf_path,
        filename,
        category="hazard-flood",
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


@router.post("/flood/preview")
def flood_preview(payload: dict = Body(...)):
    boundary = _extract_boundary(payload)
    show_raster = bool(payload.get("show_raster", False))
    return_period = int(payload.get("return_period", 100))
    try:
        risk_value, risk_class, breakdown, overlay_png = compute_flood_risk(
            boundary, show_raster, return_period
        )
    except Exception as exc:
        logger.exception("Flood preview failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    class_colors = {
        "Low": "#22c55e",
        "Moderate": "#f59e0b",
        "High": "#ef4444",
    }
    plot_color = class_colors.get(risk_class, "#22c55e")

    legend = [{"label": f"Plot Class (Polygon) - {risk_class}", "color": plot_color}]
    if show_raster:
        legend = [
            {"label": "Flood Depth (Raster) - Low", "color": "#e0f2fe"},
            {"label": "Flood Depth (Raster) - Moderate", "color": "#0ea5e9"},
            {"label": "Flood Depth (Raster) - High", "color": "#1d4ed8"},
            {"label": f"Plot Class (Polygon) - {risk_class}", "color": plot_color},
        ]

    note = "Screening-level flood risk based on global datasets."
    if not breakdown.get("data_available", True):
        note = (
            "No flood depth data available for this location at the selected return period. "
            "Try a different return period or enable local raster to inspect coverage."
        )

    response = {
        "risk_score": round(risk_value * 100, 1),
        "risk_class": risk_class,
        "mean_depth_m": round(breakdown["mean_depth_m"], 2),
        "max_depth_m": round(breakdown["max_depth_m"], 2),
        "inundation_percent": round(breakdown["inundation_fraction"] * 100, 1),
        "distance_to_river_m": round(breakdown["distance_to_river_m"], 1),
        "overlay": overlay_to_data_url(overlay_png),
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
    }
    return response


@router.post("/flood/pdf")
def flood_pdf(payload: dict = Body(...)):
    boundary = _extract_boundary(payload)
    show_raster = bool(payload.get("show_raster", False))
    return_period = int(payload.get("return_period", 100))
    try:
        risk_value, risk_class, breakdown, overlay_png = compute_flood_risk(
            boundary, show_raster, return_period
        )
    except Exception as exc:
        logger.exception("Flood PDF failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    class_colors = {
        "Low": "#22c55e",
        "Moderate": "#f59e0b",
        "High": "#ef4444",
    }
    plot_color = class_colors.get(risk_class, "#22c55e")

    os.makedirs(REPORTS_DIR, exist_ok=True)
    tmp_pdf = tempfile.NamedTemporaryFile(suffix="_flood_report.pdf", delete=False)
    pdf_path = tmp_pdf.name
    tmp_pdf.close()

    legend = [{"label": f"Plot Class (Polygon) - {risk_class}", "color": plot_color}]
    if show_raster:
        legend = [
            {"label": "Flood Depth (Raster) - Low", "color": "#e0f2fe"},
            {"label": "Flood Depth (Raster) - Moderate", "color": "#0ea5e9"},
            {"label": "Flood Depth (Raster) - High", "color": "#1d4ed8"},
            {"label": f"Plot Class (Polygon) - {risk_class}", "color": plot_color},
        ]

    note = "Screening-level flood risk based on global datasets."
    if not breakdown.get("data_available", True):
        note = (
            "No flood depth data available for this location at the selected return period. "
            "Try a different return period."
        )

    summary = {
        "risk_score": f"{round(risk_value * 100, 1)}",
        "risk_class": risk_class,
        "mean_depth_m": f"{round(breakdown['mean_depth_m'], 2)}",
        "max_depth_m": f"{round(breakdown['max_depth_m'], 2)}",
        "inundation_percent": f"{round(breakdown['inundation_fraction'] * 100, 1)}",
        "distance_to_river_m": f"{round(breakdown['distance_to_river_m'], 1)}",
        "buffer_m": "1000",
        "note": note,
        "method": (
            "Risk score uses GloFAS flood depth for the selected return period. "
            "We compute mean depth and inundation fraction inside the plot, "
            "adjust with distance to major rivers, normalize depth (0-3m), "
            "then combine: 60% depth + 25% inundation + 15% river proximity. "
            "Higher score indicates higher river flood susceptibility."
        ),
        "legend": legend,
        "return_period": str(return_period),
        "show_raster": str(show_raster),
    }
    render_flood_report_pdf(pdf_path, overlay_png, summary)
    return _pdf_response_with_r2(pdf_path, "flood_risk_report.pdf")
