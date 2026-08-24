"""Independent historical-flood validation for the combined river+pluvial flood engine.

This is NOT a regression fixture (see scratch/test_pluvial_regression.py for that). It is a
one-off evidence-gathering study: run the frozen, unmodified engine (compute_flood_risk +
compute_pluvial_risk, combined via _compute_combined_flood's max()+primary_driver logic - the
exact same code path every /hazards/flood/* endpoint uses) against a set of documented Nigerian
flood events and a set of non-flooded control locations, then check whether the flooded group
scores systematically higher than the control group.

Per explicit instruction: the engine is NOT tuned based on this study's results. This script only
observes and reports what the already-frozen methodology produces.

Coordinates are town/city-level centroids for the reported event area, not exact building
footprints - the same "regional proxy, not site-level validation" caveat that applies to every
GEE-boundary test done this session applies here too. Site selection is not random or exhaustive:
flooded sites are events with reasonably well-known approximate locations; control sites are
places with no widely-reported flood history, chosen for geographic/climatic diversity rather than
a formal absence-of-flooding database. This is suggestive evidence for a discrimination check, not
a peer-reviewed validation study.

Run: python scratch/test_historical_flood_validation.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath("."))
from dotenv import load_dotenv
load_dotenv()

from app.db import SessionLocal
from app.routers.hazards import _compute_combined_flood

SITE_PARAMS = {"site_type": None, "design_rainfall_mm": None, "analysis_mode": "hybrid"}

# (name, lat, lon, year, mechanism, source_note)
# mechanism is the reported physical driver of the ACTUAL historical event, for context only -
# the engine is scored on combined (river OR rainfall) risk regardless of mechanism, since a real
# buyer/lender cares whether the site floods at all, not which of our two internal engines caught it.
FLOODED_SITES = [
    ("Lokoja (Niger-Benue confluence)", 7.8023, 6.7333, "2012/2018/2022", "fluvial",
     "Recurrent River Niger/Benue confluence overflow; among Nigeria's most consistently flooded towns"),
    ("Port Harcourt (Niger Delta)", 4.8156, 7.0498, "recurrent", "pluvial/tidal",
     "Recurrent urban flash flooding, poor drainage + high water table"),
    ("Maiduguri (Alau Dam failure area)", 11.8333, 13.1500, "2024-09", "dam failure/fluvial",
     "Alau Dam breach, Ngadda River overflow into the city"),
    ("Makurdi (River Benue)", 7.7322, 8.5391, "2012/2017/2022", "fluvial",
     "Recurrent River Benue overbank flooding, low-lying riverside wards"),
    ("Yenagoa (Niger Delta)", 4.9247, 6.2642, "2022", "fluvial/backwater",
     "2022 Nigeria floods - one of the worst-hit state capitals"),
    ("Ogbaru (River Niger, Anambra)", 6.1667, 6.7500, "2012/2022", "fluvial",
     "Low-lying River Niger floodplain LGA, repeatedly inundated"),
    ("Ibadan (Ogunpa River corridor)", 7.3775, 3.8880, "2011-08", "pluvial/urban drainage",
     "Aug 2011 Ogunpa flash flood, one of Nigeria's deadliest urban pluvial events"),
    ("Lagos (Ajegunle)", 6.4550, 3.3260, "recurrent", "pluvial/urban drainage",
     "Recurrent low-lying urban flash flooding, minimal drainage capacity"),
    ("Mokwa (Niger State)", 9.2937, 5.0653, "2024-05", "flash flood/mixed",
     "May 2024 flash flood, one of the deadliest single flood events in recent Nigerian history"),
    ("Numan (River Benue, Adamawa)", 9.4667, 12.0333, "2022", "fluvial",
     "River Benue floodplain town, 2022 Nigeria floods"),
    ("Hadejia (Jigawa)", 12.4500, 10.0333, "2026-08", "pluvial/flash flood",
     "The event that originally prompted this whole investigation - heavy rainfall, house collapses"),
]

# (name, lat, lon, rationale)
CONTROL_SITES = [
    ("Jos (Plateau highland)", 9.8965, 8.8583, "Highland plateau town, no widely-reported flood history"),
    ("Abuja - Asokoro/Maitama", 9.0479, 7.5289, "Elevated, planned drainage infrastructure"),
    ("Kano (old city core)", 12.0022, 8.5920, "Semi-arid region, low historical flood incidence"),
    ("Enugu (Independence Layout)", 6.4413, 7.4988, "Hilly terrain, historically less flood-prone"),
    ("Sokoto (city center)", 13.0059, 5.2476, "Semi-arid, low historical flood incidence"),
    ("Ibadan - Bodija GRA", 7.4270, 3.9080, "Elevated, well-drained reservation area, contrasts directly with the Ogunpa flood zone above"),
    ("Obudu Plateau", 6.7500, 9.3500, "Rural highland, no reported flood history"),
    ("Mambilla Plateau", 6.9500, 11.3500, "Rural highland, no reported flood history"),
]


def _square_boundary(lat: float, lon: float, half_width_deg: float = 0.00035) -> dict:
    d = half_width_deg
    return {
        "type": "Polygon",
        "coordinates": [[
            [lon - d, lat - d], [lon + d, lat - d], [lon + d, lat + d], [lon - d, lat + d], [lon - d, lat - d],
        ]],
    }


def _run_group(db, sites, label):
    results = []
    print(f"\n=== {label} ===")
    for entry in sites:
        name, lat, lon = entry[0], entry[1], entry[2]
        boundary = _square_boundary(lat, lon)
        try:
            river_result, pluvial_result, overall = _compute_combined_flood(
                db, boundary, False, 100, None, SITE_PARAMS,
            )
        except Exception as exc:
            print(f"[ERROR] {name}: {exc!r}")
            continue
        river_risk, river_class, river_breakdown, _ = river_result
        pluvial_risk, pluvial_class, pluvial_breakdown, _ = pluvial_result
        river_avail = river_breakdown.get("data_available")
        print(
            f"  {name}: overall={overall['risk_value']:.3f} ({overall['risk_class']}, "
            f"driver={overall['primary_driver']}) | river={river_risk:.3f}/{river_class}"
            f"{'*no-coverage*' if not river_avail else ''} | rainfall={pluvial_risk:.3f}/{pluvial_class}"
        )
        results.append({
            "name": name, "lat": lat, "lon": lon,
            "overall_score": overall["risk_value"], "overall_class": overall["risk_class"],
            "primary_driver": overall["primary_driver"],
            "river_score": river_risk, "river_class": river_class, "river_data_available": river_avail,
            "rainfall_score": pluvial_risk, "rainfall_class": pluvial_class,
        })
    return results


def main() -> int:
    db = SessionLocal()
    try:
        flooded = _run_group(db, FLOODED_SITES, "FLOODED SITES (documented historical events)")
        control = _run_group(db, CONTROL_SITES, "CONTROL SITES (no reported flood history)")
    finally:
        db.close()

    print("\n=== SUMMARY ===")
    if not flooded or not control:
        print("Insufficient results to summarize (errors above).")
        return 1

    flooded_scores = [r["overall_score"] for r in flooded]
    control_scores = [r["overall_score"] for r in control]
    flooded_mean = sum(flooded_scores) / len(flooded_scores)
    control_mean = sum(control_scores) / len(control_scores)

    class_rank = {"Low": 0, "Moderate": 1, "High": 2, "Severe": 3, "No Data": -1}
    flooded_moderate_plus = sum(1 for r in flooded if class_rank.get(r["overall_class"], -1) >= 1)
    control_moderate_plus = sum(1 for r in control if class_rank.get(r["overall_class"], -1) >= 1)

    # Rank-based discrimination (Mann-Whitney U / AUC statistic): fraction of (flooded, control)
    # pairs where the flooded site scores strictly higher. 0.5 = no discrimination, 1.0 = perfect.
    pairs = 0
    concordant = 0
    ties = 0
    for f in flooded_scores:
        for c in control_scores:
            pairs += 1
            if f > c:
                concordant += 1
            elif f == c:
                ties += 1
    auc = (concordant + 0.5 * ties) / pairs if pairs else float("nan")

    print(f"Flooded sites (n={len(flooded)}): mean overall score = {flooded_mean:.3f}, "
          f"{flooded_moderate_plus}/{len(flooded)} classed Moderate or higher")
    print(f"Control sites (n={len(control)}): mean overall score = {control_mean:.3f}, "
          f"{control_moderate_plus}/{len(control)} classed Moderate or higher")
    print(f"Rank discrimination (AUC-style, 0.5=none / 1.0=perfect): {auc:.3f}")

    print("\nFlooded sites scoring Low (worth investigating, not tuning):")
    for r in flooded:
        if r["overall_class"] == "Low":
            print(f"  - {r['name']}: {r['overall_score']:.3f} (river={r['river_score']:.3f}, rainfall={r['rainfall_score']:.3f})")

    print("\nControl sites scoring High/Severe (false-positive candidates, worth investigating):")
    for r in control:
        if r["overall_class"] in ("High", "Severe"):
            print(f"  - {r['name']}: {r['overall_score']:.3f} (river={r['river_score']:.3f}, rainfall={r['rainfall_score']:.3f})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
