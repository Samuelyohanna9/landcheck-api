from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import FileResponse, Response
from sqlalchemy.orm import Session
import os
import tempfile
import logging

from app.db import SessionLocal
from app.utils.hazard_common import classify_risk, risk_tier_legend
from app.utils.hazard_flood import compute_flood_risk, overlay_to_data_url as flood_overlay_to_data_url
from app.utils.hazard_erosion import compute_erosion_risk, overlay_to_data_url as erosion_overlay_to_data_url
from app.utils.hazard_pdf import render_flood_report_pdf, render_erosion_report_pdf
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

def _flood_note_and_method(breakdown, pdf: bool = False) -> tuple[str, str]:
    data_source = breakdown.get("flood_data_source", "glofas")
    if data_source == "local_terrain_proxy":
        note = (
            "This site sits outside the modeled river-flood extent for the selected return period, "
            "most likely because it's too far from a major river channel. In its place, we've "
            "estimated local flood/ponding susceptibility directly from the site's terrain — the "
            "score below reflects that estimate, not a simulated river flood depth."
        )
        method = (
            "Global river-flood models carry no data for this site, so susceptibility is estimated "
            "from terrain instead, following the topographic-control principles established by "
            "Beven & Kirkby (1979) and the depression-based ponding index of Huang et al. (2019): "
            "40% low-lying terrain (elevation relative to the surrounding 300 m) + 35% flatness "
            "(slope — flat ground drains poorly) + 25% proximity to the nearest natural drainage "
            "line (HydroSHEDS). A higher score indicates greater susceptibility to standing water, "
            "not river flood depth."
        )
    elif not breakdown.get("data_available", True):
        note = (
            "We couldn't determine flood exposure for this location at the selected return period. "
            "Try a different return period." + ("" if pdf else " or enable the local risk raster to inspect coverage.")
        )
        method = (
            "Flood depth is drawn from the JRC/Copernicus GloFAS global hazard model (Dottori et "
            "al., 2016) for the selected return period. We combine mean depth inside the plot "
            "(normalized to a 3 m ceiling), the inundated area fraction, and proximity to the "
            "nearest major river channel (HydroSHEDS drainage network) into a single weighted "
            "score: 60% depth + 25% inundation + 15% river proximity. A higher score indicates "
            "greater river-flood susceptibility."
        )
    else:
        note = "Flood exposure for this site, screened against the global JRC/GloFAS river-flood hazard model."
        method = (
            "Flood depth is drawn from the JRC/Copernicus GloFAS global hazard model (Dottori et "
            "al., 2016) for the selected return period. We combine mean depth inside the plot "
            "(normalized to a 3 m ceiling), the inundated area fraction, and proximity to the "
            "nearest major river channel (HydroSHEDS drainage network) into a single weighted "
            "score: 60% depth + 25% inundation + 15% river proximity. A higher score indicates "
            "greater river-flood susceptibility."
        )
    return note, method


def _flood_preview_payload(risk_value, risk_class, class_color, breakdown, overlay_png, show_raster, return_period, legend) -> dict:
    note, method = _flood_note_and_method(breakdown)
    return {
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
        "method": method,
        "return_period": return_period,
        "data_available": bool(breakdown.get("data_available", True)),
        "legend": legend,
        "show_raster": show_raster,
        "local_elevation_used": bool(breakdown.get("local_elevation_used")),
        "relative_elevation_m": breakdown.get("relative_elevation_m"),
        "local_mean_elevation_m": breakdown.get("local_mean_elevation_m"),
        "regional_mean_elevation_m": breakdown.get("regional_mean_elevation_m"),
        "interactive": breakdown.get("_interactive"),
        "flood_data_source": breakdown.get("flood_data_source", "glofas"),
        "terrain_slope_deg": breakdown.get("terrain_slope_deg"),
        "terrain_depression_m": breakdown.get("terrain_depression_m"),
        "terrain_flatness_score": breakdown.get("terrain_flatness_score"),
        "terrain_drainage_score": breakdown.get("terrain_drainage_score"),
        "terrain_depression_score": breakdown.get("terrain_depression_score"),
        "references": breakdown.get("_references", []),
    }


def _flood_pdf_summary(risk_value, risk_class, class_color, breakdown, show_raster, return_period, legend) -> dict:
    note, _ = _flood_note_and_method(breakdown, pdf=True)
    return {
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
        "flood_data_source": breakdown.get("flood_data_source", "glofas"),
        "terrain_slope_deg": breakdown.get("terrain_slope_deg"),
        "terrain_depression_m": breakdown.get("terrain_depression_m"),
        "terrain_flatness_score": breakdown.get("terrain_flatness_score"),
        "terrain_drainage_score": breakdown.get("terrain_drainage_score"),
        "terrain_depression_score": breakdown.get("terrain_depression_score"),
        "references": breakdown.get("_references", []),
    }


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
    legend = _build_legend(risk_class, class_color, "This Site", show_raster, RASTER_LEGEND_FLOOD)
    return _flood_preview_payload(risk_value, risk_class, class_color, breakdown, overlay_png, show_raster, return_period, legend)


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
    legend = _build_legend(risk_class, class_color, "This Site", show_raster, RASTER_LEGEND_FLOOD)

    os.makedirs(REPORTS_DIR, exist_ok=True)
    tmp_pdf = tempfile.NamedTemporaryFile(suffix="_flood_report.pdf", delete=False)
    pdf_path = tmp_pdf.name
    tmp_pdf.close()

    summary = _flood_pdf_summary(risk_value, risk_class, class_color, breakdown, show_raster, return_period, legend)
    render_flood_report_pdf(pdf_path, overlay_png, summary)
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
        "references": breakdown.get("_references", []),
    }


@router.post("/erosion/preview")
def erosion_preview(payload: dict = Body(...), db: Session = Depends(get_db)):
    boundary = _extract_boundary(payload)
    show_raster = bool(payload.get("show_raster", False))
    local_elevation_points = _extract_local_elevation_points(payload)
    try:
        risk_value, risk_class, breakdown, overlay_png = compute_erosion_risk(
            db, boundary, show_raster, local_elevation_points
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
    try:
        risk_value, risk_class, breakdown, overlay_png = compute_erosion_risk(
            db, boundary, show_raster, local_elevation_points
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
    try:
        risk_value, risk_class, breakdown, _overlay_png = compute_flood_risk(
            db, boundary, False, return_period, local_elevation_points
        )
    except Exception as exc:
        logger.exception("Flood GIS export failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    export_data = breakdown.get("_gis_export") or {}
    zip_bytes = build_hazard_gis_export_zip(
        hazard_type="flood",
        boundary_geojson=export_data.get("boundary_geojson", boundary),
        buildings_gdf=export_data.get("buildings_gdf"),
        value_points=export_data.get("value_points"),
        value_key=export_data.get("value_key", "depth_m"),
        risk_class=risk_class,
        risk_score=round(risk_value * 100, 1),
        method_text=(
            f"Flood depth sampled from JRC/CEMS GloFAS Flood Hazard v2.1 at the "
            f"{return_period}-year return period. Buildings are OpenStreetMap footprints, "
            "flagged as threatened where interpolated depth exceeds 5cm."
        ),
    )
    return _gis_export_response(zip_bytes, "flood_hazard_gis_export.zip")


@router.post("/erosion/gis-export")
def erosion_gis_export(payload: dict = Body(...), db: Session = Depends(get_db)):
    boundary = _extract_boundary(payload)
    local_elevation_points = _extract_local_elevation_points(payload)
    try:
        risk_value, risk_class, breakdown, _overlay_png = compute_erosion_risk(
            db, boundary, False, local_elevation_points
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

        set_hazard_job_status(db, job_id, status="running", stage="Starting analysis...", progress_pct=1, started=True)
        report = make_progress_reporter(db, job_id)

        if hazard_type == "flood":
            risk_value, risk_class, breakdown, overlay_png = compute_flood_risk(
                db, boundary, show_raster, return_period, local_elevation_points, progress_cb=report
            )
        else:
            risk_value, risk_class, breakdown, overlay_png = compute_erosion_risk(
                db, boundary, show_raster, local_elevation_points, progress_cb=report
            )

        _, class_color = classify_risk(risk_value, breakdown.get("data_available", True))
        raster_legend = RASTER_LEGEND_FLOOD if hazard_type == "flood" else RASTER_LEGEND_EROSION
        legend = _build_legend(risk_class, class_color, "This Site", show_raster, raster_legend)

        if output_type == "preview":
            if hazard_type == "flood":
                result = _flood_preview_payload(risk_value, risk_class, class_color, breakdown, overlay_png, show_raster, return_period, legend)
            else:
                result = _erosion_preview_payload(risk_value, risk_class, class_color, breakdown, overlay_png, show_raster, legend)
            set_hazard_job_status(db, job_id, status="completed", stage="Complete", progress_pct=100, result_payload=result, completed=True)

        elif output_type == "pdf":
            os.makedirs(REPORTS_DIR, exist_ok=True)
            tmp_pdf = tempfile.NamedTemporaryFile(suffix=f"_{hazard_type}_report.pdf", delete=False)
            pdf_path = tmp_pdf.name
            tmp_pdf.close()
            if hazard_type == "flood":
                summary = _flood_pdf_summary(risk_value, risk_class, class_color, breakdown, show_raster, return_period, legend)
                render_flood_report_pdf(pdf_path, overlay_png, summary)
            else:
                summary = _erosion_pdf_summary(risk_value, risk_class, class_color, breakdown, show_raster, legend)
                render_erosion_report_pdf(pdf_path, overlay_png, summary)
            with open(pdf_path, "rb") as f:
                file_bytes = f.read()
            try:
                os.remove(pdf_path)
            except OSError:
                pass
            file_name = "flood_risk_report.pdf" if hazard_type == "flood" else "erosion_risk_report.pdf"
            set_hazard_job_status(
                db, job_id, status="completed", stage="Complete", progress_pct=100,
                file_bytes=file_bytes, file_name=file_name, content_type="application/pdf", completed=True,
            )

        elif output_type == "gis-export":
            export_data = breakdown.get("_gis_export") or {}
            if hazard_type == "flood":
                zip_bytes = build_hazard_gis_export_zip(
                    hazard_type="flood",
                    boundary_geojson=export_data.get("boundary_geojson", boundary),
                    buildings_gdf=export_data.get("buildings_gdf"),
                    value_points=export_data.get("value_points"),
                    value_key=export_data.get("value_key", "depth_m"),
                    risk_class=risk_class,
                    risk_score=round(risk_value * 100, 1),
                    method_text=(
                        f"Flood depth sampled from JRC/CEMS GloFAS Flood Hazard v2.1 at the "
                        f"{return_period}-year return period. Buildings are OpenStreetMap footprints, "
                        "flagged as threatened where interpolated depth exceeds 5cm."
                    ),
                )
                file_name = "flood_hazard_gis_export.zip"
            else:
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
    if hazard_type not in ("flood", "erosion"):
        raise HTTPException(status_code=404, detail="Unknown hazard type")
    output_type = str(payload.get("output_type") or "preview")
    if output_type not in ("preview", "pdf", "gis-export"):
        raise HTTPException(status_code=400, detail="Unsupported output_type")
    request_payload = {
        "geometry": _extract_boundary(payload),
        "show_raster": bool(payload.get("show_raster", False)),
        "return_period": int(payload.get("return_period", 100)),
        "local_elevation_points": _extract_local_elevation_points(payload),
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
