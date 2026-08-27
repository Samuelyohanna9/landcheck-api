from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

import ee
from shapely import wkb
from shapely.geometry.base import BaseGeometry
from sqlalchemy import text
from sqlalchemy.orm import Session

# Shared 4-tier risk scale used by every hazard module (flood, erosion, ...) so a client sees
# one consistent vocabulary and palette across the whole hazard report suite, not a different
# scale per hazard type.
RISK_TIERS = [
    (0.25, "Low", "#22c55e"),
    (0.50, "Moderate", "#f59e0b"),
    (0.75, "High", "#f97316"),
    (1.01, "Severe", "#ef4444"),
]
NO_DATA_COLOR = "#94a3b8"

# Real, peer-reviewed sources grounding each screening methodology - surfaced to users in both
# the PDF report and the web UI so the analysis is auditable, not a black box. Each entry is a
# complete, independently verifiable citation (not a placeholder); HydroSHEDS appears in more than
# one list because the same drainage-network dataset genuinely underlies more than one factor.
HYDROSHEDS_REFERENCE = {
    "short": "Lehner et al. (2008)",
    "citation": "Lehner, B., Verdin, K., Jarvis, A. (2008). New global hydrography derived from spaceborne elevation data. Eos, Transactions American Geophysical Union, 89(10), 93–94.",
    "url": "https://doi.org/10.1029/2008EO100001",
}

FLOOD_REFERENCES_GLOFAS = [
    {
        "short": "Dottori et al. (2016)",
        "citation": "Dottori, F., Salamon, P., Bianchi, A., Alfieri, L., Hirpa, F.A., Feyen, L. (2016). Development and evaluation of a framework for global flood hazard mapping. Advances in Water Resources, 94, 87–102.",
        "url": "https://doi.org/10.1016/j.advwatres.2016.05.002",
    },
    HYDROSHEDS_REFERENCE,
]

FLOOD_REFERENCES_TERRAIN_PROXY = [
    {
        "short": "Beven & Kirkby (1979)",
        "citation": "Beven, K.J., Kirkby, M.J. (1979). A physically based, variable contributing area model of basin hydrology. Hydrological Sciences Bulletin, 24(1), 43–69.",
        "url": "https://doi.org/10.1080/02626667909491834",
    },
    {
        "short": "Huang et al. (2019)",
        "citation": "Huang, H.B., Chen, X., Wang, X.W., Wang, X.N., Liu, L. (2019). A Depression-Based Index to Represent Topographic Control in Urban Pluvial Flooding. Water, 11(10), 2115.",
        "url": "https://doi.org/10.3390/w11102115",
    },
    HYDROSHEDS_REFERENCE,
]

EROSION_REFERENCES = [
    {
        "short": "Renard et al. (1997)",
        "citation": "Renard, K.G., Foster, G.R., Weesies, G.A., McCool, D.K., Yoder, D.C. (1997). Predicting Soil Erosion by Water: A Guide to Conservation Planning with the Revised Universal Soil Loss Equation (RUSLE). USDA Agriculture Handbook No. 703.",
        "url": "https://www.ars.usda.gov/ARSUserFiles/64080530/RUSLE/AH_703.pdf",
    },
    {
        "short": "Van der Knijff et al. (2000)",
        "citation": "Van der Knijff, J.M., Jones, R.J.A., Montanarella, L. (2000). Soil Erosion Risk Assessment in Europe. EUR 19044 EN, European Commission Joint Research Centre.",
        "url": "https://esdac.jrc.ec.europa.eu/ESDB_Archive/pesera/pesera_cd/pdf/ereurnew2.pdf",
    },
    {
        "short": "Igwe et al. (2020)",
        "citation": "Igwe, O., John, U.I., Solomon, O., Obinna, O. (2020). GIS-based gully erosion susceptibility modeling, adapting bivariate statistical method and AHP approach in Gombe town and environs, Northeast Nigeria. Geoenvironmental Disasters, 7, 32.",
        "url": "https://doi.org/10.1186/s40677-020-00166-8",
    },
    HYDROSHEDS_REFERENCE,
]


def classify_risk(
    value: float, data_available: bool = True, tiers: Optional[List[Tuple[float, str, str]]] = None,
) -> Tuple[str, str]:
    """Maps a 0-1 risk score to (label, hex color). data_available=False always returns the
    "No Data" tier regardless of value, since a 0.0 score from missing data must never be
    displayed the same way as a genuine "Low" score.

    `tiers` overrides the shared RISK_TIERS - used by branches whose score distribution doesn't
    match the shared cutoffs' assumptions (currently only Floodplain; see hazards.py's
    _FLOODPLAIN_RISK_TIERS for why) without forking this whole function.
    """
    if not data_available:
        return "No Data", NO_DATA_COLOR
    safe_value = max(0.0, min(1.0, float(value)))
    for ceiling, label, color in (tiers or RISK_TIERS):
        if safe_value < ceiling:
            return label, color
    return "Severe", "#ef4444"


def risk_tier_legend() -> list[dict]:
    labels_seen = set()
    legend = []
    for _, label, color in RISK_TIERS:
        if label in labels_seen:
            continue
        labels_seen.add(label)
        legend.append({"label": label, "color": color})
    return legend


def fetch_susceptibility_points(image: "ee.Image", region: "ee.Geometry", scale_m: int = 90) -> Optional[List[Dict[str, float]]]:
    """Samples a 0-1 susceptibility image as a {lng, lat, flood_susceptibility_pct} point cloud for
    a map's graduated surface - same pixelLonLat + toList idiom used throughout this app's hazard
    modules. Shared by hazard_pluvial.py and hazard_floodplain.py (two independent engines each
    producing their own 0-1 susceptibility surface); lives here rather than in the pure-rendering
    hazard_map_renderer.py, which deliberately has zero direct Earth Engine coupling.
    """
    try:
        sampled = image.multiply(100).rename("flood_susceptibility_pct").addBands(ee.Image.pixelLonLat())
        reduced = sampled.reduceRegion(
            reducer=ee.Reducer.toList(),
            geometry=region,
            scale=scale_m,
            maxPixels=int(1e9),
            bestEffort=True,
        )
        info = reduced.getInfo() or {}
        lons = info.get("longitude") or []
        lats = info.get("latitude") or []
        values = info.get("flood_susceptibility_pct") or []
        if len(lons) < 3 or len(lons) != len(lats) or len(lons) != len(values):
            return None
        points = [
            {"lng": float(lo), "lat": float(la), "flood_susceptibility_pct": float(v)}
            for lo, la, v in zip(lons, lats, values)
            if v is not None
        ]
        return points if len(points) >= 3 else None
    except Exception:
        return None


def fetch_buildings_near(db: Session, boundary_geojson: Dict[str, Any], buffer_m: float = 500, limit: int = 4000) -> List[BaseGeometry]:
    """Real OSM building footprint polygons (EPSG:4326) intersecting a metric buffer around a
    hazard boundary - the same `multipolygons` table Survey Plan's Auto Feature Detection already
    reads from (see plots.py's _run_plot_feature_detection), just queried directly against an
    arbitrary boundary instead of a saved plot row.

    The row limit exists so a plot covering a whole dense town can't turn a single hazard request
    into an unbounded-size query-and-plot. ORDER BY random() matters as much as the limit itself:
    without it, Postgres returns whichever rows its index scan happens to visit first, which is
    NOT spatially representative - for a large area that meant the truncated set could visually
    look like only one corner of the whole plot had any buildings at all, when in reality they
    were spread everywhere. Random ordering makes the truncated sample statistically match the
    real spatial density everywhere, at the cost of a full sort of the matched rows (still fast -
    the GiST index on geom already does the heavy filtering before this sort ever runs).
    """
    rows = db.execute(
        text(
            """
            SELECT m.geom
            FROM multipolygons m
            WHERE m.building IS NOT NULL
              AND ST_Intersects(
                  m.geom,
                  ST_Buffer(
                      ST_SetSRID(ST_GeomFromGeoJSON(:boundary_geojson), 4326)::geography,
                      :buffer_m
                  )::geometry
              )
            ORDER BY random()
            LIMIT :limit
            """
        ),
        {"boundary_geojson": json.dumps(boundary_geojson), "buffer_m": buffer_m, "limit": limit},
    ).fetchall()
    geometries = []
    for row in rows:
        try:
            geometries.append(wkb.loads(row[0]))
        except Exception:
            continue
    return geometries
