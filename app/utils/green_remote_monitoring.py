from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import ee

from app.utils.gee_client import init_gee


VEGETATION_NDVI_THRESHOLD = 0.35
DEFAULT_SUMMARY_WINDOW_DAYS = 90


@dataclass
class RemoteMonitoringPeriod:
    label: str
    start_date: date
    end_date: date


def _as_date(value: date | datetime | str | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text_value = str(value or "").strip()
    if not text_value:
        return None
    try:
        return datetime.fromisoformat(text_value[:10]).date()
    except Exception:
        return None


def _first_day_of_month(value: date) -> date:
    return value.replace(day=1)


def _next_month(value: date) -> date:
    if value.month == 12:
        return date(value.year + 1, 1, 1)
    return date(value.year, value.month + 1, 1)


def _mask_sentinel_clouds(image: ee.Image) -> ee.Image:
    qa = image.select("QA60")
    cloud_mask = qa.bitwiseAnd(1 << 10).eq(0)
    cirrus_mask = qa.bitwiseAnd(1 << 11).eq(0)
    return image.updateMask(cloud_mask.And(cirrus_mask)).copyProperties(image, image.propertyNames())


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except Exception:
        return None
    return numeric if numeric == numeric else None


def _build_period_stats(
    geom: ee.Geometry,
    *,
    start_date: date,
    end_date: date,
    tree_count: int,
    polygon_area_sqm: float | None,
    vegetation_threshold: float,
) -> dict[str, Any]:
    collection = (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterBounds(geom)
        .filterDate(start_date.isoformat(), end_date.isoformat())
        .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 80))
        .map(_mask_sentinel_clouds)
    )

    image_count = int(collection.size().getInfo() or 0)
    if image_count <= 0:
        return {
            "image_count": 0,
            "latest_image_date": None,
            "mean_ndvi": None,
            "vegetation_area_sqm": None,
            "vegetation_coverage_pct": None,
            "vegetation_area_per_tree_sqm": None,
            "clear_area_sqm": None,
            "clear_coverage_pct": None,
        }

    composite = collection.median()
    ndvi = composite.normalizedDifference(["B8", "B4"]).rename("ndvi")
    pixel_area = ee.Image.pixelArea().rename("pixel_area")

    total_area = (
        pixel_area.reduceRegion(
            reducer=ee.Reducer.sum(),
            geometry=geom,
            scale=10,
            maxPixels=1e9,
        ).get("pixel_area")
    )
    clear_area = (
        pixel_area.updateMask(ndvi.mask()).reduceRegion(
            reducer=ee.Reducer.sum(),
            geometry=geom,
            scale=10,
            maxPixels=1e9,
        ).get("pixel_area")
    )
    vegetation_area = (
        pixel_area.updateMask(ndvi.gte(vegetation_threshold)).reduceRegion(
            reducer=ee.Reducer.sum(),
            geometry=geom,
            scale=10,
            maxPixels=1e9,
        ).get("pixel_area")
    )
    mean_ndvi = (
        ndvi.reduceRegion(
            reducer=ee.Reducer.mean(),
            geometry=geom,
            scale=10,
            maxPixels=1e9,
        ).get("ndvi")
    )
    latest_image = ee.Image(collection.sort("system:time_start", False).first())
    latest_image_date = latest_image.date().format("YYYY-MM-dd")

    total_area_value = _safe_float(total_area.getInfo() if hasattr(total_area, "getInfo") else total_area)
    clear_area_value = _safe_float(clear_area.getInfo() if hasattr(clear_area, "getInfo") else clear_area)
    vegetation_area_value = _safe_float(
        vegetation_area.getInfo() if hasattr(vegetation_area, "getInfo") else vegetation_area
    )
    mean_ndvi_value = _safe_float(mean_ndvi.getInfo() if hasattr(mean_ndvi, "getInfo") else mean_ndvi)
    latest_image_value = str(latest_image_date.getInfo() or "").strip() if hasattr(latest_image_date, "getInfo") else None

    area_reference = polygon_area_sqm if polygon_area_sqm and polygon_area_sqm > 0 else total_area_value
    vegetation_pct = (
        (vegetation_area_value / area_reference) * 100
        if vegetation_area_value is not None and area_reference and area_reference > 0
        else None
    )
    clear_pct = (
        (clear_area_value / area_reference) * 100
        if clear_area_value is not None and area_reference and area_reference > 0
        else None
    )
    vegetation_per_tree = (
        vegetation_area_value / tree_count
        if vegetation_area_value is not None and tree_count > 0
        else None
    )

    return {
        "image_count": image_count,
        "latest_image_date": latest_image_value or None,
        "mean_ndvi": round(mean_ndvi_value, 4) if mean_ndvi_value is not None else None,
        "vegetation_area_sqm": round(vegetation_area_value, 2) if vegetation_area_value is not None else None,
        "vegetation_coverage_pct": round(vegetation_pct, 2) if vegetation_pct is not None else None,
        "vegetation_area_per_tree_sqm": round(vegetation_per_tree, 2) if vegetation_per_tree is not None else None,
        "clear_area_sqm": round(clear_area_value, 2) if clear_area_value is not None else None,
        "clear_coverage_pct": round(clear_pct, 2) if clear_pct is not None else None,
    }


def _build_monthly_periods(*, baseline_date: date | None, months: int) -> list[RemoteMonitoringPeriod]:
    today = date.today()
    current_month = _first_day_of_month(today)
    start_month = current_month
    for _ in range(max(months - 1, 0)):
        previous_day = start_month - timedelta(days=1)
        start_month = previous_day.replace(day=1)
    if baseline_date:
        baseline_month = _first_day_of_month(baseline_date)
        if baseline_month > start_month:
            start_month = baseline_month

    periods: list[RemoteMonitoringPeriod] = []
    cursor = start_month
    limit = current_month
    while cursor <= limit:
        next_cursor = _next_month(cursor)
        periods.append(
            RemoteMonitoringPeriod(
                label=cursor.strftime("%b %Y"),
                start_date=cursor,
                end_date=next_cursor,
            )
        )
        cursor = next_cursor
    return periods[-max(months, 1) :]


def classify_remote_monitoring_signal(
    *,
    summary_per_tree: float | None,
    summary_ndvi: float | None,
    history_per_tree: list[float],
) -> tuple[str, str]:
    if summary_per_tree is None and summary_ndvi is None:
        return "no_data", "No recent cloud-free vegetation signal is available for this polygon."

    if summary_per_tree is None:
        if summary_ndvi is not None and summary_ndvi < 0.2:
            return "watch", "Current vegetation signal is weak for this polygon."
        return "stable", "Vegetation signal is available. Use tree count and coverage together for interpretation."

    if summary_per_tree <= 0:
        return "watch", "No meaningful vegetated area was detected in the latest monitoring window."

    previous = [value for value in history_per_tree[:-1] if value is not None and value > 0]
    if previous:
        recent_baseline = sum(previous[-3:]) / min(len(previous), 3)
        if recent_baseline > 0 and summary_per_tree < recent_baseline * 0.75:
            return "watch", "Vegetation per tree dropped below the recent block baseline. Check this area in the field."
        if recent_baseline > 0 and summary_per_tree > recent_baseline * 1.1:
            return "improving", "Vegetation per tree is trending above the recent block baseline."

    if summary_ndvi is not None and summary_ndvi < 0.2:
        return "watch", "Current NDVI is low for this block."
    return "stable", "Vegetation signal is stable for this block."


def compute_remote_monitoring_report(
    *,
    boundary_geojson: dict[str, Any],
    tree_count: int,
    polygon_area_sqm: float | None = None,
    baseline_date: date | datetime | str | None = None,
    series_months: int = 6,
    summary_window_days: int = DEFAULT_SUMMARY_WINDOW_DAYS,
    vegetation_threshold: float = VEGETATION_NDVI_THRESHOLD,
) -> dict[str, Any]:
    init_gee()
    geom = ee.Geometry(boundary_geojson)
    baseline = _as_date(baseline_date)
    today = date.today()
    summary_start = today - timedelta(days=max(int(summary_window_days or DEFAULT_SUMMARY_WINDOW_DAYS), 30))
    summary_end = today + timedelta(days=1)

    summary = _build_period_stats(
        geom,
        start_date=summary_start,
        end_date=summary_end,
        tree_count=max(int(tree_count or 0), 0),
        polygon_area_sqm=polygon_area_sqm,
        vegetation_threshold=vegetation_threshold,
    )

    periods = _build_monthly_periods(baseline_date=baseline, months=max(1, min(int(series_months or 6), 12)))
    series: list[dict[str, Any]] = []
    for period in periods:
        period_stats = _build_period_stats(
            geom,
            start_date=period.start_date,
            end_date=period.end_date,
            tree_count=max(int(tree_count or 0), 0),
            polygon_area_sqm=polygon_area_sqm,
            vegetation_threshold=vegetation_threshold,
        )
        series.append(
            {
                "label": period.label,
                "start_date": period.start_date.isoformat(),
                "end_date": period.end_date.isoformat(),
                **period_stats,
            }
        )

    history_per_tree = [
        float(item["vegetation_area_per_tree_sqm"])
        for item in series
        if item.get("vegetation_area_per_tree_sqm") is not None
    ]
    signal, signal_message = classify_remote_monitoring_signal(
        summary_per_tree=summary.get("vegetation_area_per_tree_sqm"),
        summary_ndvi=summary.get("mean_ndvi"),
        history_per_tree=history_per_tree,
    )

    return {
        "summary": {
            **summary,
            "signal": signal,
            "signal_message": signal_message,
            "summary_window_start": summary_start.isoformat(),
            "summary_window_end": summary_end.isoformat(),
            "vegetation_threshold_ndvi": vegetation_threshold,
        },
        "series": series,
    }
