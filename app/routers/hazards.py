from fastapi import APIRouter, Body, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from sqlalchemy.orm import Session
import geopandas as gpd
import json
import os
import shutil
import tempfile
import logging

from app.db import SessionLocal
from app.utils.hazard_common import classify_risk, risk_tier_legend
from app.utils.hazard_flood import compute_flood_risk, overlay_to_data_url as flood_overlay_to_data_url
from app.utils.hazard_pluvial import compute_pluvial_risk, pluvial_overlay_to_data_url
from app.utils.hazard_erosion import compute_erosion_risk, overlay_to_data_url as erosion_overlay_to_data_url
from app.utils.hazard_lulc import compute_lulc_summary, overlay_to_data_url as lulc_overlay_to_data_url
from app.utils.hazard_pdf import render_flood_report_pdf, render_erosion_report_pdf, render_lulc_report_pdf
from app.utils.hazard_gis_export import build_hazard_gis_export_zip
from app.utils.hazard_jobs import (
    get_hazard_job,
    get_hazard_job_file,
    insert_hazard_job,
    make_progress_reporter,
    serialize_hazard_job,
    set_hazard_job_status,
)
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
    {"label": "Flood Depth - Low", "color": "#e0f2fe"},
    {"label": "Flood Depth - Moderate", "color": "#0ea5e9"},
    {"label": "Flood Depth - High", "color": "#1d4ed8"},
]
RASTER_LEGEND_EROSION = [
    {"label": "Slope - Gentle", "color": "#fef9c3"},
    {"label": "Slope - Moderate", "color": "#f97316"},
    {"label": "Slope - Steep", "color": "#7c2d12"},
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


# Optional per-point fields from an uploaded geotechnical/soil survey - none are required, and any
# missing/invalid value on a given point is simply omitted from that point rather than rejecting the
# whole point, since a real field survey rarely has every reading on every station.
_OPTIONAL_SOIL_FIELDS = (
    "silt_vfs_pct", "clay_pct", "sand_pct", "organic_matter_pct",
    "soil_structure_code", "soil_permeability_code",
    "cohesion_kpa", "friction_angle_deg", "plasticity_index",
)


def _extract_local_elevation_points(payload: dict) -> list:
    # elevation_m is intentionally optional here (unlike lng/lat): a point carrying only
    # geotechnical/soil fields - no surveyed elevation - is still valid input for the
    # gully-susceptibility/K-factor/HSG calculations, which don't need elevation at all. Points
    # that do have elevation_m keep driving the existing local-slope/relative-elevation features.
    points = payload.get("local_elevation_points") or payload.get("elevation_points") or []
    if not isinstance(points, list):
        return []
    valid = []
    for p in points:
        if not isinstance(p, dict):
            continue
        try:
            entry = {
                "lng": float(p["lng"]),
                "lat": float(p["lat"]),
            }
        except (KeyError, TypeError, ValueError):
            continue
        if p.get("elevation_m") is not None:
            try:
                entry["elevation_m"] = float(p["elevation_m"])
            except (TypeError, ValueError):
                pass
        for field in _OPTIONAL_SOIL_FIELDS:
            if p.get(field) is None:
                continue
            try:
                entry[field] = float(p[field])
            except (TypeError, ValueError):
                pass
        valid.append(entry)
    return valid


def _extract_site_params(payload: dict) -> dict:
    site_type = payload.get("site_type")
    design_rainfall_mm = payload.get("design_rainfall_mm")
    try:
        design_rainfall_mm = float(design_rainfall_mm) if design_rainfall_mm is not None else None
    except (TypeError, ValueError):
        design_rainfall_mm = None
    analysis_mode = str(payload.get("analysis_mode") or "hybrid")
    if analysis_mode not in ("satellite", "local", "hybrid"):
        analysis_mode = "hybrid"
    return {
        "site_type": str(site_type) if site_type else None,
        "design_rainfall_mm": design_rainfall_mm,
        "analysis_mode": analysis_mode,
    }


def _build_legend(risk_class: str, class_color: str, plot_label: str, show_raster: bool, raster_legend: list) -> list:
    legend = [{"label": f"{plot_label} - {risk_class}", "color": class_color}]
    legend.extend(risk_tier_legend())
    if show_raster:
        legend.extend(raster_legend)
    return legend


# ---------------- FLOOD ----------------
# Two independent, always-computed engines - River Flood Risk (compute_flood_risk, JRC/GloFAS
# fluvial modeling) and Surface-Water/Rainfall Flood Susceptibility (compute_pluvial_risk, terrain+
# CHIRPS-rainfall+soil+built-surface pluvial modeling) - combined via max(), never averaged, so a
# severe reading in either mechanism can never be diluted by a low reading in the other. Every
# response shows both sections separately plus which one is driving the overall figure, rather than
# collapsing them into one opaque blended score.

def _river_note_and_method(breakdown: dict, pdf: bool = False) -> tuple[str, str]:
    method = (
        "Flood depth is drawn from the JRC/Copernicus GloFAS global hazard model (Dottori et "
        "al., 2016) for the selected return period. We combine mean depth inside the plot "
        "(normalized to a 3 m ceiling), the inundated area fraction, and proximity to the "
        "nearest major river channel (HydroSHEDS drainage network) into a single weighted "
        "score: 60% depth + 25% inundation + 15% river proximity. A higher score indicates "
        "greater river/fluvial flood susceptibility."
    )
    if breakdown.get("data_available", True):
        note = "River flood exposure for this site, screened against the global JRC/GloFAS river-flood hazard model."
    else:
        # Stated as a checked, confirmed absence - not "we couldn't determine", which reads as if
        # data might exist but wasn't found. GloFAS only carries data within its own modeled river-
        # flood extent; most plots not near a major mapped river legitimately have none here.
        note = "No modelled GloFAS river-flood inundation was detected at this location for the selected return period."
    return note, method


def _pluvial_note_and_method(breakdown: dict) -> tuple[str, str]:
    site_type_source = breakdown.get("site_type_source")
    site_note = (
        " automatically inferred from built-up land cover" if site_type_source == "auto_lulc"
        else " supplied by the surveyor" if site_type_source == "user_input" else ""
    )
    note = (
        "Surface-water/rainfall flood susceptibility - whether intense rainfall is likely to pond "
        "or run across this land, independent of whether a river ever reaches it. This is a "
        "susceptibility assessment based on terrain, land cover, soil, and historical extreme-"
        "rainfall characteristics, not a prediction that any specific future storm will flood the "
        "property."
    )
    method = (
        "Combines terrain ponding susceptibility (40% low-lying terrain relative to the surrounding "
        "300 m + 35% flatness + 25% proximity to the nearest natural drainage line - HydroSHEDS - "
        "following Beven & Kirkby, 1979, and Huang et al., 2019), a rainfall-runoff estimate (SCS/"
        "NRCS Curve Number method, USDA NEH-4/TR-55, driven by the 99th-percentile daily rainfall "
        "from 40+ years of CHIRPS satellite-rainfall estimates - Funk et al., 2015 - and a "
        "Hydrologic Soil Group derived from global soil texture data) and impervious/built-up "
        "surface fraction (Esri 10m Land Cover" + site_note + ") into a single weighted score: "
        "35% terrain susceptibility + 35% runoff + 30% impervious surface. The design-storm "
        "rainfall figure is an empirical historical extreme, not a formal engineered intensity-"
        "duration-frequency curve - a rarer, more severe individual storm can still exceed it."
    )
    return note, method


def _flood_overall_note(river_breakdown: dict, pluvial_breakdown: dict, primary_driver: str) -> str:
    river_available = bool(river_breakdown.get("data_available"))
    if primary_driver == "rainfall" and not river_available:
        return (
            "No modelled GloFAS river-flood inundation was detected at this location. Overall flood "
            "risk here is driven by surface-water/rainfall susceptibility (see below), not a river "
            "overflow scenario."
        )
    if primary_driver == "rainfall":
        return "Surface-water/rainfall flooding is the primary driver of flood risk at this site, exceeding the river/fluvial component."
    return "River/fluvial flooding is the primary driver of flood risk at this site."


def _flood_river_section(risk_value, risk_class, class_color, breakdown, overlay_png, return_period, pdf: bool = False) -> dict:
    note, method = _river_note_and_method(breakdown, pdf=pdf)
    fmt = (lambda v: str(v)) if pdf else (lambda v: v)
    return {
        "risk_score": fmt(round(risk_value * 100, 1)),
        "risk_class": risk_class,
        "class_color": class_color,
        "mean_depth_m": fmt(round(breakdown["mean_depth_m"], 2)),
        "max_depth_m": fmt(round(breakdown["max_depth_m"], 2)),
        "inundation_percent": fmt(round(breakdown["inundation_fraction"] * 100, 1)),
        "distance_to_river_m": fmt(round(breakdown["distance_to_river_m"], 1)),
        "depth_score": breakdown["depth_score"],
        "inundation_score": breakdown["inundation_score"],
        "river_proximity_score": breakdown["river_proximity_score"],
        "buildings_total": breakdown.get("buildings_total", 0),
        "buildings_threatened": breakdown.get("buildings_threatened", 0),
        "overlay": None if pdf else flood_overlay_to_data_url(overlay_png),
        "note": note,
        "method": method,
        "return_period": return_period,
        "data_available": bool(breakdown.get("data_available", True)),
        "local_elevation_used": bool(breakdown.get("local_elevation_used")),
        "relative_elevation_m": breakdown.get("relative_elevation_m"),
        "local_mean_elevation_m": breakdown.get("local_mean_elevation_m"),
        "regional_mean_elevation_m": breakdown.get("regional_mean_elevation_m"),
        "interactive": breakdown.get("_interactive"),
        "flood_data_source": breakdown.get("flood_data_source", "glofas"),
        "data_sources": breakdown.get("data_sources"),
        "confidence": breakdown.get("confidence"),
        "references": breakdown.get("_references", []),
    }


def _flood_rainfall_section(risk_value, risk_class, class_color, breakdown, overlay_png, pdf: bool = False) -> dict:
    note, method = _pluvial_note_and_method(breakdown)
    fmt = (lambda v: str(v)) if pdf else (lambda v: v)
    return {
        "risk_score": fmt(round(risk_value * 100, 1)),
        "risk_class": risk_class,
        "class_color": class_color,
        "susceptibility_pct": fmt(breakdown.get("susceptibility_pct")),
        "design_rainfall_mm": fmt(breakdown.get("design_rainfall_mm")),
        "runoff_coefficient": fmt(breakdown.get("runoff_coefficient")),
        "impervious_fraction_pct": fmt(breakdown.get("impervious_fraction_pct")),
        "terrain_score": breakdown.get("terrain_score"),
        "runoff_score": breakdown.get("runoff_score"),
        "impervious_score": breakdown.get("impervious_score"),
        "terrain_slope_deg": breakdown.get("terrain_slope_deg"),
        "terrain_depression_m": breakdown.get("terrain_depression_m"),
        "distance_to_drainage_m": breakdown.get("distance_to_drainage_m"),
        "scs_runoff": breakdown.get("scs_runoff"),
        "hydrologic_soil_group": breakdown.get("hydrologic_soil_group"),
        "site_type_used": breakdown.get("site_type_used"),
        "site_type_source": breakdown.get("site_type_source"),
        "buildings_total": breakdown.get("buildings_total", 0),
        "buildings_threatened": breakdown.get("buildings_threatened", 0),
        "overlay": None if pdf else pluvial_overlay_to_data_url(overlay_png),
        "note": note,
        "method": method,
        "data_available": bool(breakdown.get("data_available", True)),
        "interactive": breakdown.get("_interactive"),
        "data_sources": breakdown.get("data_sources"),
        "confidence": breakdown.get("confidence"),
        "analysis_mode": breakdown.get("analysis_mode", "hybrid"),
        "references": breakdown.get("_references", []),
    }


def _compute_combined_flood(
    db, boundary, show_raster, return_period, local_elevation_points, site_params, progress_cb=None,
):
    """Runs both flood engines and combines them - the one place this logic lives, called by every
    flood endpoint (sync preview/pdf/gis-export and the async job runner) so the max()+primary_driver
    +classify_risk behavior can never silently diverge between them.
    """
    river_risk, river_class, river_breakdown, river_png = compute_flood_risk(
        db, boundary, show_raster, return_period, local_elevation_points, progress_cb=progress_cb,
    )
    pluvial_risk, pluvial_class, pluvial_breakdown, pluvial_png = compute_pluvial_risk(
        db, boundary, local_elevation_points,
        site_type=site_params["site_type"], design_rainfall_mm=site_params["design_rainfall_mm"],
        analysis_mode=site_params["analysis_mode"], progress_cb=progress_cb,
    )
    risk_value = max(river_risk, pluvial_risk)
    primary_driver = "river" if river_risk >= pluvial_risk else "rainfall"
    combined_data_available = bool(river_breakdown.get("data_available")) or bool(pluvial_breakdown.get("data_available"))
    risk_class, class_color = classify_risk(risk_value, combined_data_available)

    river_result = (river_risk, river_class, river_breakdown, river_png)
    pluvial_result = (pluvial_risk, pluvial_class, pluvial_breakdown, pluvial_png)
    overall = {
        "risk_value": risk_value,
        "risk_class": risk_class,
        "class_color": class_color,
        "primary_driver": primary_driver,
        "data_available": combined_data_available,
    }
    return river_result, pluvial_result, overall


def _flood_combined_payload(river_result, pluvial_result, overall, show_raster, return_period) -> dict:
    river_risk, river_class, river_breakdown, river_png = river_result
    pluvial_risk, pluvial_class, pluvial_breakdown, pluvial_png = pluvial_result
    _, river_color = classify_risk(river_risk, river_breakdown.get("data_available", True))
    _, pluvial_color = classify_risk(pluvial_risk, pluvial_breakdown.get("data_available", True))
    legend = _build_legend(overall["risk_class"], overall["class_color"], "This Site", show_raster, RASTER_LEGEND_FLOOD)
    return {
        "overall": {
            "risk_score": round(overall["risk_value"] * 100, 1),
            "risk_class": overall["risk_class"],
            "class_color": overall["class_color"],
            "primary_driver": overall["primary_driver"],
            "data_available": overall["data_available"],
            "note": _flood_overall_note(river_breakdown, pluvial_breakdown, overall["primary_driver"]),
        },
        "river": _flood_river_section(river_risk, river_class, river_color, river_breakdown, river_png, return_period),
        "rainfall": _flood_rainfall_section(pluvial_risk, pluvial_class, pluvial_color, pluvial_breakdown, pluvial_png),
        "local_elevation_used": bool(river_breakdown.get("local_elevation_used")),
        "relative_elevation_m": river_breakdown.get("relative_elevation_m"),
        "legend": legend,
        "buffer_m": 1000,
        "show_raster": show_raster,
        "local_data_gaps": [],
    }


def _flood_combined_pdf_summary(river_result, pluvial_result, overall, show_raster, return_period) -> dict:
    river_risk, river_class, river_breakdown, _river_png = river_result
    pluvial_risk, pluvial_class, pluvial_breakdown, _pluvial_png = pluvial_result
    _, river_color = classify_risk(river_risk, river_breakdown.get("data_available", True))
    _, pluvial_color = classify_risk(pluvial_risk, pluvial_breakdown.get("data_available", True))
    legend = _build_legend(overall["risk_class"], overall["class_color"], "This Site", show_raster, RASTER_LEGEND_FLOOD)
    return {
        "overall": {
            "risk_score": f"{round(overall['risk_value'] * 100, 1)}",
            "risk_class": overall["risk_class"],
            "class_color": overall["class_color"],
            "primary_driver": overall["primary_driver"],
            "note": _flood_overall_note(river_breakdown, pluvial_breakdown, overall["primary_driver"]),
        },
        "river": _flood_river_section(river_risk, river_class, river_color, river_breakdown, None, return_period, pdf=True),
        "rainfall": _flood_rainfall_section(pluvial_risk, pluvial_class, pluvial_color, pluvial_breakdown, None, pdf=True),
        "legend": legend,
        "show_raster": str(show_raster),
    }


@router.post("/flood/preview")
def flood_preview(payload: dict = Body(...), db: Session = Depends(get_db)):
    boundary = _extract_boundary(payload)
    show_raster = bool(payload.get("show_raster", False))
    return_period = int(payload.get("return_period", 100))
    local_elevation_points = _extract_local_elevation_points(payload)
    site_params = _extract_site_params(payload)
    try:
        river_result, pluvial_result, overall = _compute_combined_flood(
            db, boundary, show_raster, return_period, local_elevation_points, site_params,
        )
    except Exception as exc:
        logger.exception("Flood preview failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return _flood_combined_payload(river_result, pluvial_result, overall, show_raster, return_period)


@router.post("/flood/pdf")
def flood_pdf(payload: dict = Body(...), db: Session = Depends(get_db)):
    boundary = _extract_boundary(payload)
    show_raster = bool(payload.get("show_raster", False))
    return_period = int(payload.get("return_period", 100))
    local_elevation_points = _extract_local_elevation_points(payload)
    site_params = _extract_site_params(payload)
    try:
        river_result, pluvial_result, overall = _compute_combined_flood(
            db, boundary, show_raster, return_period, local_elevation_points, site_params,
        )
    except Exception as exc:
        logger.exception("Flood PDF failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    os.makedirs(REPORTS_DIR, exist_ok=True)
    tmp_pdf = tempfile.NamedTemporaryFile(suffix="_flood_report.pdf", delete=False)
    pdf_path = tmp_pdf.name
    tmp_pdf.close()

    summary = _flood_combined_pdf_summary(river_result, pluvial_result, overall, show_raster, return_period)
    render_flood_report_pdf(pdf_path, river_result[3], pluvial_result[3], summary)
    return _pdf_response_with_r2(pdf_path, "flood_risk_report.pdf", "hazard-flood")


# ---------------- EROSION ----------------

def _erosion_preview_payload(risk_value, risk_class, class_color, breakdown, overlay_png, show_raster, legend) -> dict:
    note = "Erosion susceptibility for this site, screened against global terrain and vegetation-cover data."
    if not breakdown.get("data_available", True):
        note = "We couldn't determine erosion susceptibility for this location. Try a different site or contact support."
    method = (
        "A susceptibility index adapted from the RUSLE erosion-factor framework (Renard et al., "
        "1997) rather than a full soil-loss estimate. Slope is sampled from a global 30 m "
        "elevation model, vegetation cover from recent cloud-free Sentinel-2 imagery using the "
        "NDVI-based cover method of Van der Knijff et al. (2000), and drainage concentration from "
        "the HydroSHEDS flow-accumulation network — the same drainage-buffer factor used in "
        "Nigeria-specific gully-erosion susceptibility modeling (Igwe et al., 2020). These combine "
        "into a single weighted score: 50% slope + 30% bare-ground exposure + 20% drainage "
        "concentration. A higher score indicates greater erosion susceptibility."
    )
    return {
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
        "method": method,
        "data_available": bool(breakdown.get("data_available", True)),
        "legend": legend,
        "show_raster": show_raster,
        "slope_source": breakdown.get("slope_source", "unavailable"),
        "buildings_total": breakdown.get("buildings_total", 0),
        "buildings_threatened": breakdown.get("buildings_threatened", 0),
        "interactive": breakdown.get("_interactive"),
        "local_soil_data_available": bool(breakdown.get("local_soil_data_available")),
        "gully_susceptibility_index": breakdown.get("gully_susceptibility_index"),
        "k_factor": breakdown.get("k_factor"),
        "analysis_mode": breakdown.get("analysis_mode", "hybrid"),
        "data_sources": breakdown.get("data_sources"),
        "confidence": breakdown.get("confidence"),
        "local_data_gaps": breakdown.get("local_data_gaps", []),
        "references": breakdown.get("_references", []),
    }


def _erosion_pdf_summary(risk_value, risk_class, class_color, breakdown, show_raster, legend) -> dict:
    note = "Erosion susceptibility for this site, screened against global terrain and vegetation-cover data."
    if not breakdown.get("data_available", True):
        note = "We couldn't determine erosion susceptibility for this location."
    return {
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
        "buildings_total": breakdown.get("buildings_total", 0),
        "buildings_threatened": breakdown.get("buildings_threatened", 0),
        "local_soil_data_available": bool(breakdown.get("local_soil_data_available")),
        "gully_susceptibility_index": breakdown.get("gully_susceptibility_index"),
        "k_factor": breakdown.get("k_factor"),
        "analysis_mode": breakdown.get("analysis_mode", "hybrid"),
        "data_sources": breakdown.get("data_sources"),
        "confidence": breakdown.get("confidence"),
        "local_data_gaps": breakdown.get("local_data_gaps", []),
        "references": breakdown.get("_references", []),
    }


@router.post("/erosion/preview")
def erosion_preview(payload: dict = Body(...), db: Session = Depends(get_db)):
    boundary = _extract_boundary(payload)
    show_raster = bool(payload.get("show_raster", False))
    local_elevation_points = _extract_local_elevation_points(payload)
    site_params = _extract_site_params(payload)
    try:
        risk_value, risk_class, breakdown, overlay_png = compute_erosion_risk(
            db, boundary, show_raster, local_elevation_points,
            analysis_mode=site_params["analysis_mode"],
        )
    except Exception as exc:
        logger.exception("Erosion preview failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    _, class_color = classify_risk(risk_value, breakdown.get("data_available", True))
    legend = _build_legend(risk_class, class_color, "This Site", show_raster, RASTER_LEGEND_EROSION)
    return _erosion_preview_payload(risk_value, risk_class, class_color, breakdown, overlay_png, show_raster, legend)


@router.post("/erosion/pdf")
def erosion_pdf(payload: dict = Body(...), db: Session = Depends(get_db)):
    boundary = _extract_boundary(payload)
    show_raster = bool(payload.get("show_raster", False))
    local_elevation_points = _extract_local_elevation_points(payload)
    site_params = _extract_site_params(payload)
    try:
        risk_value, risk_class, breakdown, overlay_png = compute_erosion_risk(
            db, boundary, show_raster, local_elevation_points,
            analysis_mode=site_params["analysis_mode"],
        )
    except Exception as exc:
        logger.exception("Erosion PDF failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    _, class_color = classify_risk(risk_value, breakdown.get("data_available", True))
    legend = _build_legend(risk_class, class_color, "This Site", show_raster, RASTER_LEGEND_EROSION)

    os.makedirs(REPORTS_DIR, exist_ok=True)
    tmp_pdf = tempfile.NamedTemporaryFile(suffix="_erosion_report.pdf", delete=False)
    pdf_path = tmp_pdf.name
    tmp_pdf.close()

    summary = _erosion_pdf_summary(risk_value, risk_class, class_color, breakdown, show_raster, legend)
    render_erosion_report_pdf(pdf_path, overlay_png, summary)
    return _pdf_response_with_r2(pdf_path, "erosion_risk_report.pdf", "hazard-erosion")


# ---------------- LAND USE / LAND COVER ----------------
# Architecturally the odd one out among the three hazard types: purely informational (no risk
# score/tier/badge - land cover isn't itself a hazard), no OSM buildings, no local-survey inputs.
# compute_lulc_summary() returns (breakdown, overlay_png) - a 2-tuple, not flood/erosion's 4-tuple -
# so it can't share their result-unpacking line in _run_hazard_analysis_job below.

def _lulc_preview_payload(breakdown: dict, overlay_png: bytes) -> dict:
    return {
        "class_areas": breakdown.get("class_areas", []),
        "class_count": breakdown.get("class_count", 0),
        "dominant_class": breakdown.get("dominant_class"),
        "dominant_pct": breakdown.get("dominant_pct"),
        "total_area_ha": breakdown.get("total_area_ha"),
        "overlay": lulc_overlay_to_data_url(overlay_png),
        "note": "Land cover composition for this site, from Esri's 10m Annual Land Cover dataset.",
        "buffer_m": breakdown.get("buffer_m", 500),
        "data_available": bool(breakdown.get("data_available", True)),
        "legend": breakdown.get("legend", []),
        "interactive": breakdown.get("_interactive"),
        "references": breakdown.get("_references", []),
    }


def _lulc_pdf_summary(breakdown: dict) -> dict:
    return {
        "class_areas": breakdown.get("class_areas", []),
        "class_count": breakdown.get("class_count", 0),
        "dominant_class": breakdown.get("dominant_class"),
        "dominant_pct": breakdown.get("dominant_pct"),
        "total_area_ha": breakdown.get("total_area_ha"),
        "note": "Land cover composition for this site, from Esri's 10m Annual Land Cover dataset.",
        "buffer_m": breakdown.get("buffer_m", 500),
        "legend": breakdown.get("legend", []),
        "references": breakdown.get("_references", []),
    }


@router.post("/lulc/preview")
def lulc_preview(payload: dict = Body(...), db: Session = Depends(get_db)):
    boundary = _extract_boundary(payload)
    try:
        breakdown, overlay_png = compute_lulc_summary(db, boundary)
    except Exception as exc:
        logger.exception("LULC preview failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return _lulc_preview_payload(breakdown, overlay_png)


@router.post("/lulc/pdf")
def lulc_pdf(payload: dict = Body(...), db: Session = Depends(get_db)):
    boundary = _extract_boundary(payload)
    try:
        breakdown, overlay_png = compute_lulc_summary(db, boundary)
    except Exception as exc:
        logger.exception("LULC PDF failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    os.makedirs(REPORTS_DIR, exist_ok=True)
    tmp_pdf = tempfile.NamedTemporaryFile(suffix="_lulc_report.pdf", delete=False)
    pdf_path = tmp_pdf.name
    tmp_pdf.close()

    summary = _lulc_pdf_summary(breakdown)
    render_lulc_report_pdf(pdf_path, overlay_png, summary)
    return _pdf_response_with_r2(pdf_path, "lulc_report.pdf", "hazard-lulc")


# ---------------- GIS EXPORT ----------------

def _gis_export_response(zip_bytes: bytes, filename: str) -> Response:
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/flood/gis-export")
def flood_gis_export(payload: dict = Body(...), db: Session = Depends(get_db)):
    boundary = _extract_boundary(payload)
    return_period = int(payload.get("return_period", 100))
    local_elevation_points = _extract_local_elevation_points(payload)
    site_params = _extract_site_params(payload)
    # River and rainfall are two different value surfaces (depth in metres vs. susceptibility in
    # percent) - build_hazard_gis_export_zip's value_key/value_points shape assumes one surface per
    # export, so this exports one engine at a time, defaulting to "river" (the pre-existing default
    # behavior, for any caller that doesn't yet know about the two-engine split).
    engine = str(payload.get("engine") or "river")
    if engine not in ("river", "rainfall"):
        raise HTTPException(status_code=400, detail="engine must be 'river' or 'rainfall'")
    try:
        if engine == "river":
            risk_value, risk_class, breakdown, _overlay_png = compute_flood_risk(
                db, boundary, False, return_period, local_elevation_points,
            )
            method_text = (
                f"Flood depth sampled from JRC/CEMS GloFAS Flood Hazard v2.1 at the "
                f"{return_period}-year return period. Buildings are OpenStreetMap footprints, "
                "flagged as threatened where interpolated depth exceeds 5cm."
            )
            file_name = "flood_river_hazard_gis_export.zip"
        else:
            risk_value, risk_class, breakdown, _overlay_png = compute_pluvial_risk(
                db, boundary, local_elevation_points,
                site_type=site_params["site_type"], design_rainfall_mm=site_params["design_rainfall_mm"],
                analysis_mode=site_params["analysis_mode"],
            )
            method_text = (
                "Surface-water/rainfall susceptibility from terrain, CHIRPS extreme rainfall, soil "
                "infiltration, and built-up surface fraction. Buildings are OpenStreetMap footprints, "
                "flagged where they sit on susceptible ground - not a river-flood simulation."
            )
            file_name = "flood_rainfall_susceptibility_gis_export.zip"
    except Exception as exc:
        logger.exception("Flood GIS export failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    export_data = breakdown.get("_gis_export") or {}
    zip_bytes = build_hazard_gis_export_zip(
        hazard_type="flood",
        boundary_geojson=export_data.get("boundary_geojson", boundary),
        buildings_gdf=export_data.get("buildings_gdf"),
        value_points=export_data.get("value_points"),
        value_key=export_data.get("value_key", "depth_m" if engine == "river" else "flood_susceptibility_pct"),
        risk_class=risk_class,
        risk_score=round(risk_value * 100, 1),
        method_text=method_text,
    )
    return _gis_export_response(zip_bytes, file_name)


@router.post("/erosion/gis-export")
def erosion_gis_export(payload: dict = Body(...), db: Session = Depends(get_db)):
    boundary = _extract_boundary(payload)
    local_elevation_points = _extract_local_elevation_points(payload)
    site_params = _extract_site_params(payload)
    try:
        risk_value, risk_class, breakdown, _overlay_png = compute_erosion_risk(
            db, boundary, False, local_elevation_points,
            analysis_mode=site_params["analysis_mode"],
        )
    except Exception as exc:
        logger.exception("Erosion GIS export failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    export_data = breakdown.get("_gis_export") or {}
    slope_source = breakdown.get("slope_source", "unavailable")
    zip_bytes = build_hazard_gis_export_zip(
        hazard_type="erosion",
        boundary_geojson=export_data.get("boundary_geojson", boundary),
        buildings_gdf=export_data.get("buildings_gdf"),
        value_points=export_data.get("value_points"),
        value_key=export_data.get("value_key", "slope_deg"),
        risk_class=risk_class,
        risk_score=round(risk_value * 100, 1),
        method_text=(
            "Slope susceptibility screening. "
            + ("Slope points are from the surveyor's own uploaded elevation data."
               if slope_source == "local_survey"
               else "Slope points are sampled from a global 30m Copernicus DEM.")
            + " Buildings are OpenStreetMap footprints, flagged as threatened where on slope > 15 degrees."
        ),
    )
    return _gis_export_response(zip_bytes, "erosion_hazard_gis_export.zip")


# ---------------- BOUNDARY UPLOAD ----------------

MAX_BOUNDARY_UPLOAD_BYTES = 20 * 1024 * 1024  # a boundary file is inherently tiny; this just guards against an unrelated large upload


@router.post("/upload-boundary")
async def upload_hazard_boundary(file: UploadFile = File(...)):
    """Accepts a zipped Shapefile (.zip), GeoJSON (.geojson/.json), or KML (.kml) and returns the
    parsed area as a single WGS84 GeoJSON geometry - the same shape _extract_boundary already
    expects from a manually drawn/coordinate-entered boundary, so an uploaded file feeds the exact
    same flood/erosion analysis pipeline as any other boundary source.
    """
    filename = str(file.filename or "boundary")
    suffix = os.path.splitext(filename)[1].lower()
    if suffix not in (".zip", ".geojson", ".json", ".kml"):
        raise HTTPException(status_code=400, detail="Upload a zipped Shapefile (.zip), GeoJSON (.geojson/.json), or KML (.kml) file.")

    payload = await file.read()
    if not payload:
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")
    if len(payload) > MAX_BOUNDARY_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail=f"File exceeds the {MAX_BOUNDARY_UPLOAD_BYTES // (1024 * 1024)} MB upload limit.")

    tmp_dir = tempfile.mkdtemp(prefix="hazard_boundary_")
    try:
        tmp_path = os.path.join(tmp_dir, f"boundary{suffix}")
        with open(tmp_path, "wb") as f:
            f.write(payload)
        try:
            gdf = gpd.read_file(tmp_path)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Couldn't parse this file as a boundary: {exc}") from exc
        if gdf.empty:
            raise HTTPException(status_code=400, detail="No geometry found in the uploaded file.")
        if gdf.crs is None:
            gdf = gdf.set_crs(epsg=4326)
        elif str(gdf.crs) != "EPSG:4326":
            gdf = gdf.to_crs(epsg=4326)
        union_geom = gdf.geometry.union_all() if hasattr(gdf.geometry, "union_all") else gdf.geometry.unary_union
        if union_geom is None or union_geom.is_empty:
            raise HTTPException(status_code=400, detail="No usable geometry found in the uploaded file.")
        boundary_geojson = json.loads(gpd.GeoSeries([union_geom], crs="EPSG:4326").to_json())["features"][0]["geometry"]
        return {"boundary": boundary_geojson}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------- ASYNC JOBS ----------------
# A single request that runs several Earth Engine calls plus local rendering can take longer than
# a client's request timeout allows, especially on a slower server - these endpoints hand the work
# to a background thread immediately and let the client poll for status/progress instead, which
# also lets the frontend show real stage-by-stage progress rather than a blank spinner.

def _run_hazard_analysis_job(job_id: str) -> None:
    db = SessionLocal()
    try:
        job = get_hazard_job(db, job_id)
        if not job:
            return
        payload = job.get("request_payload") or {}
        if isinstance(payload, str):
            import json
            payload = json.loads(payload)
        hazard_type = job.get("hazard_type")
        output_type = job.get("output_type")
        boundary = _extract_boundary(payload)
        show_raster = bool(payload.get("show_raster", False))
        return_period = int(payload.get("return_period", 100))
        local_elevation_points = _extract_local_elevation_points(payload)
        site_params = _extract_site_params(payload)

        set_hazard_job_status(db, job_id, status="running", stage="Starting analysis...", progress_pct=1, started=True)
        report = make_progress_reporter(db, job_id)

        # LULC returns a different shape (2-tuple: breakdown, overlay_png - no risk_value/risk_class,
        # since land cover isn't itself a hazard) and so can't share flood/erosion's unpacking line.
        # Flood returns two full results (river + rainfall engines) rather than one, so it uses its
        # own river_result/pluvial_result/overall variables instead of the shared risk_value/
        # risk_class/class_color/breakdown/overlay_png used by erosion/lulc below.
        risk_value = risk_class = class_color = legend = None
        river_result = pluvial_result = overall = None
        if hazard_type == "flood":
            river_result, pluvial_result, overall = _compute_combined_flood(
                db, boundary, show_raster, return_period, local_elevation_points, site_params, progress_cb=report,
            )
        elif hazard_type == "erosion":
            risk_value, risk_class, breakdown, overlay_png = compute_erosion_risk(
                db, boundary, show_raster, local_elevation_points,
                analysis_mode=site_params["analysis_mode"], progress_cb=report,
            )
            _, class_color = classify_risk(risk_value, breakdown.get("data_available", True))
            legend = _build_legend(risk_class, class_color, "This Site", show_raster, RASTER_LEGEND_EROSION)
        else:  # lulc
            breakdown, overlay_png = compute_lulc_summary(db, boundary, progress_cb=report)
            legend = breakdown.get("legend", [])

        if output_type == "preview":
            if hazard_type == "flood":
                result = _flood_combined_payload(river_result, pluvial_result, overall, show_raster, return_period)
            elif hazard_type == "erosion":
                result = _erosion_preview_payload(risk_value, risk_class, class_color, breakdown, overlay_png, show_raster, legend)
            else:
                result = _lulc_preview_payload(breakdown, overlay_png)
            set_hazard_job_status(db, job_id, status="completed", stage="Complete", progress_pct=100, result_payload=result, completed=True)

        elif output_type == "pdf":
            os.makedirs(REPORTS_DIR, exist_ok=True)
            tmp_pdf = tempfile.NamedTemporaryFile(suffix=f"_{hazard_type}_report.pdf", delete=False)
            pdf_path = tmp_pdf.name
            tmp_pdf.close()
            if hazard_type == "flood":
                summary = _flood_combined_pdf_summary(river_result, pluvial_result, overall, show_raster, return_period)
                render_flood_report_pdf(pdf_path, river_result[3], pluvial_result[3], summary)
                file_name = "flood_risk_report.pdf"
            elif hazard_type == "erosion":
                summary = _erosion_pdf_summary(risk_value, risk_class, class_color, breakdown, show_raster, legend)
                render_erosion_report_pdf(pdf_path, overlay_png, summary)
                file_name = "erosion_risk_report.pdf"
            else:
                summary = _lulc_pdf_summary(breakdown)
                render_lulc_report_pdf(pdf_path, overlay_png, summary)
                file_name = "lulc_report.pdf"
            with open(pdf_path, "rb") as f:
                file_bytes = f.read()
            try:
                os.remove(pdf_path)
            except OSError:
                pass
            set_hazard_job_status(
                db, job_id, status="completed", stage="Complete", progress_pct=100,
                file_bytes=file_bytes, file_name=file_name, content_type="application/pdf", completed=True,
            )

        elif output_type == "gis-export":
            if hazard_type == "lulc":
                # Categorical land cover has no buildings/risk-score/value-surface to export in the
                # shape build_hazard_gis_export_zip expects - out of scope for v1 rather than
                # exporting something misleading. create_hazard_analysis_job already rejects this
                # combination with an immediate 400 before a job is even queued; this is just the
                # defense-in-depth backstop if that endpoint-level guard is ever bypassed.
                raise ValueError("GIS export is not available for land cover analysis")
            if hazard_type == "flood":
                # River and rainfall are two different value surfaces - export one at a time,
                # selected the same way the sync /flood/gis-export endpoint does (default "river").
                engine = str(payload.get("engine") or "river")
                if engine not in ("river", "rainfall"):
                    raise ValueError("engine must be 'river' or 'rainfall'")
                if engine == "river":
                    engine_risk, engine_class, engine_breakdown, _png = river_result
                    method_text = (
                        f"Flood depth sampled from JRC/CEMS GloFAS Flood Hazard v2.1 at the "
                        f"{return_period}-year return period. Buildings are OpenStreetMap footprints, "
                        "flagged as threatened where interpolated depth exceeds 5cm."
                    )
                    default_value_key = "depth_m"
                    file_name = "flood_river_hazard_gis_export.zip"
                else:
                    engine_risk, engine_class, engine_breakdown, _png = pluvial_result
                    method_text = (
                        "Surface-water/rainfall susceptibility from terrain, CHIRPS extreme rainfall, "
                        "soil infiltration, and built-up surface fraction. Buildings are OpenStreetMap "
                        "footprints, flagged where they sit on susceptible ground - not a river-flood "
                        "simulation."
                    )
                    default_value_key = "flood_susceptibility_pct"
                    file_name = "flood_rainfall_susceptibility_gis_export.zip"
                export_data = engine_breakdown.get("_gis_export") or {}
                zip_bytes = build_hazard_gis_export_zip(
                    hazard_type="flood",
                    boundary_geojson=export_data.get("boundary_geojson", boundary),
                    buildings_gdf=export_data.get("buildings_gdf"),
                    value_points=export_data.get("value_points"),
                    value_key=export_data.get("value_key", default_value_key),
                    risk_class=engine_class,
                    risk_score=round(engine_risk * 100, 1),
                    method_text=method_text,
                )
            else:
                export_data = breakdown.get("_gis_export") or {}
                slope_source = breakdown.get("slope_source", "unavailable")
                zip_bytes = build_hazard_gis_export_zip(
                    hazard_type="erosion",
                    boundary_geojson=export_data.get("boundary_geojson", boundary),
                    buildings_gdf=export_data.get("buildings_gdf"),
                    value_points=export_data.get("value_points"),
                    value_key=export_data.get("value_key", "slope_deg"),
                    risk_class=risk_class,
                    risk_score=round(risk_value * 100, 1),
                    method_text=(
                        "Slope susceptibility screening. "
                        + ("Slope points are from the surveyor's own uploaded elevation data."
                           if slope_source == "local_survey"
                           else "Slope points are sampled from a global 30m Copernicus DEM.")
                        + " Buildings are OpenStreetMap footprints, flagged as threatened where on slope > 15 degrees."
                    ),
                )
                file_name = "erosion_hazard_gis_export.zip"
            set_hazard_job_status(
                db, job_id, status="completed", stage="Complete", progress_pct=100,
                file_bytes=zip_bytes, file_name=file_name, content_type="application/zip", completed=True,
            )
        else:
            raise ValueError(f"Unsupported hazard job output_type: {output_type}")

    except Exception as exc:
        logger.exception("Hazard analysis job %s failed", job_id)
        try:
            set_hazard_job_status(db, job_id, status="failed", stage="Failed", error_text=str(exc), completed=True)
        except Exception:
            db.rollback()
    finally:
        db.close()


@router.post("/{hazard_type}/analyze")
def create_hazard_analysis_job(hazard_type: str, payload: dict = Body(...), db: Session = Depends(get_db)):
    if hazard_type not in ("flood", "erosion", "lulc"):
        raise HTTPException(status_code=404, detail="Unknown hazard type")
    output_type = str(payload.get("output_type") or "preview")
    if output_type not in ("preview", "pdf", "gis-export"):
        raise HTTPException(status_code=400, detail="Unsupported output_type")
    if hazard_type == "lulc" and output_type == "gis-export":
        # Categorical land cover has no buildings/risk-score/value-surface to export in the shape
        # build_hazard_gis_export_zip expects - rejected immediately here (rather than after
        # queuing and polling a job that would always fail) - see _run_hazard_analysis_job's
        # gis-export branch for the defense-in-depth backstop.
        raise HTTPException(status_code=400, detail="GIS export is not available for land cover analysis")
    request_payload = {
        "geometry": _extract_boundary(payload),
        "show_raster": bool(payload.get("show_raster", False)),
        "return_period": int(payload.get("return_period", 100)),
        "local_elevation_points": _extract_local_elevation_points(payload),
        "engine": str(payload.get("engine") or "river"),  # flood gis-export only: "river" | "rainfall"
        **_extract_site_params(payload),
    }
    job = insert_hazard_job(
        db,
        hazard_type=hazard_type,
        output_type=output_type,
        request_payload=request_payload,
        worker=_run_hazard_analysis_job,
    )
    return serialize_hazard_job(job)


@router.get("/jobs/{job_id}")
def get_hazard_analysis_job(job_id: str, db: Session = Depends(get_db)):
    job = get_hazard_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return serialize_hazard_job(job)


@router.get("/jobs/{job_id}/download")
def download_hazard_analysis_job(job_id: str, db: Session = Depends(get_db)):
    row = get_hazard_job_file(db, job_id)
    if not row or row.get("status") != "completed" or not row.get("file_bytes"):
        raise HTTPException(status_code=404, detail="File not ready")
    return Response(
        content=bytes(row["file_bytes"]),
        media_type=row.get("content_type") or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{row.get("file_name") or "export.bin"}"'},
    )
