"""Pluvial V3 R&D - Nigeria Satellite-Event Coincidence Scan. NO production code touched, NO model
trained, NO V3.1 calculation. This deliberately reverses the search direction that stalled on
Ondo/Minna/Lagos: instead of finding well-documented floods and asking whether Sentinel-1 happened
to observe them, this finds every date/city where a Sentinel-1 acquisition ALREADY coincided with
locally-extreme rainfall, across 19 major Nigerian urban centres and the full Sentinel-1 record
(2015-present). Only the survivors of this computational screen are worth spending news-research
effort on.

PRE-REGISTERED CRITERIA (fixed before any result is inspected, per instruction):
  1. Authoritative/documented flood event - checked AFTER this scan, not here.
  2. Predominantly rainfall/drainage mechanism - checked AFTER this scan, not here.
  3. Sentinel-1 acquisition during or <=24h after a locally-extreme rainfall day (primary);
     24-48h kept as a secondary/weaker tier, not a top-tier candidate.
  4. Sufficient geographic specificity to identify affected locations - checked AFTER this scan.

This script only screens for criterion 3 (the computational part) plus a same-orbit pre-event
scene existing within a reasonable prior window (necessary for change detection at all - a
Sentinel-1 acquisition alone is useless without a same-orbit baseline to difference against).
"Locally-extreme rainfall" = CHIRPS daily precipitation, at the city point, exceeding that city's
own 95th-percentile daily value (self-referential threshold, not an absolute mm cutoff, so it
doesn't unfairly favour naturally-wetter cities) - the SAME CHIRPS asset already used in production
(hazard_pluvial.py's design-storm calculation), just applied diagnostically here.

Server-side computation via .map()/.filter() on the S1 and CHIRPS collections keeps this to
roughly one .getInfo() per city rather than one per candidate scene.

Run: python scratch/v3_nigeria_satellite_event_coincidence_scan.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.abspath("."))
from dotenv import load_dotenv
load_dotenv()

import ee
from app.utils.gee_client import init_gee


def _s1_collection(aoi):
    return (
        ee.ImageCollection("COPERNICUS/S1_GRD")
        .filterBounds(aoi)
        .filter(ee.Filter.eq("instrumentMode", "IW"))
        .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV"))
        .select("VV")
    )


def _has_same_orbit_pre_event(aoi, event_date_str, orbit):
    """Cheap existence check (not a full mask) - a same-orbit scene must exist in the
    PRE_EVENT_LOOKBACK_DAYS window before the event for change detection to even be attemptable."""
    event_dt = ee.Date(event_date_str)
    pre_col = (
        _s1_collection(aoi)
        .filterDate(event_dt.advance(-PRE_EVENT_LOOKBACK_DAYS, "day"), event_dt)
        .filter(ee.Filter.eq("relativeOrbitNumber_start", orbit))
    )
    return pre_col.size().getInfo() > 0

SEARCH_START = "2015-01-01"
SEARCH_END = "2026-08-25"  # today
RAINFALL_PERCENTILE = 95
AOI_RADIUS_M = 8000  # wide enough to cover a metro area's rain-gauge-scale CHIRPS pixel
PRE_EVENT_LOOKBACK_DAYS = 45  # how far back to search for a same-orbit pre-event scene
TOP_N_PER_TIER = 6  # keep only the highest-rainfall candidates per city/tier after same-orbit filtering - a
# reviewable shortlist, not a raw dump (Lagos alone had 81 primary-tier raw hits before this)

CITIES = {
    "Lagos_Ikeja": (6.6018, 3.3515), "Abuja": (9.0765, 7.3986),
    "PortHarcourt": (4.8156, 7.0498), "BeninCity": (6.3350, 5.6037),
    "Warri": (5.5560, 5.7932), "Calabar": (4.9517, 8.3220),
    "Uyo": (5.0377, 7.9128), "Aba": (5.1066, 7.3667),
    "Owerri": (5.4840, 7.0351), "Enugu": (6.5244, 7.5086),
    "Ibadan": (7.3775, 3.9470), "Ilorin": (8.4966, 4.5426),
    "Akure": (7.2571, 5.2058), "Osogbo": (7.7719, 4.5570),
    "Abeokuta": (7.1475, 3.3619), "Kaduna": (10.5105, 7.4165),
    "Kano": (12.0022, 8.5920), "OndoTown": (7.0889, 4.7991),
    "Minna": (9.6139, 6.5569),
}

OUT_PATH = os.path.join(os.path.dirname(__file__), "v3_nigeria_coincidence_scan_results.json")


def main() -> int:
    init_gee()
    chirps_daily = ee.ImageCollection("UCSB-CHG/CHIRPS/DAILY").select("precipitation")

    all_results = {}
    for city, (lat, lon) in CITIES.items():
        print(f"\n=== {city} ({lat},{lon}) ===", flush=True)
        pt = ee.Geometry.Point([lon, lat])
        aoi = pt.buffer(AOI_RADIUS_M)

        # City's own rainfall-extremity threshold (self-referential, matches production's
        # CHIRPS-percentile design-storm pattern, not a fixed mm cutoff).
        p_img = chirps_daily.reduce(ee.Reducer.percentile([RAINFALL_PERCENTILE])).rename("p_thresh")
        p_thresh = p_img.reduceRegion(ee.Reducer.mean(), aoi, scale=5500, maxPixels=1e9, bestEffort=True).get("p_thresh")

        s1 = _s1_collection(aoi).filterDate(SEARCH_START, SEARCH_END)
        n_total = s1.size().getInfo()

        def _tag_rain(img, p_thresh=p_thresh):
            img = ee.Image(img)
            t = ee.Date(img.get("system:time_start"))
            # Rain "same day or the day before" the acquisition - the acquisition captures ground
            # state AFTER whatever rain already fell; window covers day-of and 1 day prior.
            # CHIRPS DAILY has a real latency gap - it does not extend all the way to "today" (a
            # live 400 error was hit on a 2026-08-04 S1C scene: the summed window had zero images,
            # so the reduceRegion dictionary had no "precipitation" key at all). -9999 is used as an
            # explicit "no CHIRPS coverage" sentinel rather than letting the computation crash or
            # silently treating a coverage gap as "zero rain" (0 would wrongly exclude the scene
            # from candidacy instead of flagging it as unevaluable).
            window_rain = chirps_daily.filterDate(t.advance(-1, "day"), t.advance(1, "day")).sum()
            rain_mm = window_rain.reduceRegion(ee.Reducer.mean(), aoi, scale=5500, maxPixels=1e9, bestEffort=True).get("precipitation", -9999)
            # Secondary 24-48h tier: rain 1-2 days before.
            window_rain2 = chirps_daily.filterDate(t.advance(-2, "day"), t.advance(-1, "day")).sum()
            rain_mm_2 = window_rain2.reduceRegion(ee.Reducer.mean(), aoi, scale=5500, maxPixels=1e9, bestEffort=True).get("precipitation", -9999)
            return img.set({
                "rain_0_24h_mm": rain_mm, "rain_24_48h_mm": rain_mm_2, "p_thresh": p_thresh,
                "acq_date": t.format("YYYY-MM-dd"),
                "orbit": img.get("relativeOrbitNumber_start"),
            })

        s1_tagged = s1.map(_tag_rain)
        primary = s1_tagged.filter(ee.Filter.gt("rain_0_24h_mm", ee.Number(p_thresh)))
        secondary = s1_tagged.filter(
            ee.Filter.And(
                ee.Filter.gt("rain_24_48h_mm", ee.Number(p_thresh)),
                ee.Filter.lte("rain_0_24h_mm", ee.Number(p_thresh)),
            )
        )

        n_primary = primary.size().getInfo()
        n_secondary = secondary.size().getInfo()
        print(f"  total S1 scenes: {n_total} | primary (<=24h) hits: {n_primary} | secondary (24-48h) hits: {n_secondary}", flush=True)

        p_thresh_val = ee.Number(p_thresh).getInfo()
        print(f"  local p{RAINFALL_PERCENTILE} daily rainfall threshold: {p_thresh_val:.1f}mm", flush=True)

        results = {"p95_threshold_mm": round(p_thresh_val, 1), "n_total_s1_scenes": n_total, "primary": [], "secondary": []}

        for tier_name, tier_col, n, rank_field in (
            ("primary", primary, n_primary, "rain_0_24h_mm"),
            ("secondary", secondary, n_secondary, "rain_24_48h_mm"),
        ):
            if n == 0:
                continue
            props = tier_col.toList(n).map(lambda img: ee.Image(img).toDictionary(
                ["acq_date", "orbit", "rain_0_24h_mm", "rain_24_48h_mm", "system:index"]
            ))
            props_info = props.getInfo()
            # Rank by rainfall intensity within the tier, keep only the top N - a reviewable
            # shortlist rather than a raw dump (some cities had 50-80+ raw hits before this).
            props_info.sort(key=lambda p: p.get(rank_field) or -9999, reverse=True)
            kept, skipped_no_pre = 0, 0
            for p in props_info:
                if kept >= TOP_N_PER_TIER:
                    break
                rain_val = p.get(rank_field)
                if rain_val is None or rain_val < 0:  # -9999 sentinel = no CHIRPS coverage
                    continue
                has_pre = _has_same_orbit_pre_event(aoi, p["acq_date"], p["orbit"])
                if not has_pre:
                    skipped_no_pre += 1
                    continue
                results[tier_name].append({
                    "date": p["acq_date"], "orbit": p["orbit"],
                    "rain_0_24h_mm": round(p["rain_0_24h_mm"], 1) if p.get("rain_0_24h_mm", -9999) >= 0 else None,
                    "rain_24_48h_mm": round(p["rain_24_48h_mm"], 1) if p.get("rain_24_48h_mm", -9999) >= 0 else None,
                    "scene_id": p["system:index"], "has_same_orbit_pre_event": True,
                })
                kept += 1
            print(f"  {tier_name}: {n} raw hits -> kept top {kept} (with same-orbit pre-event available), "
                  f"{skipped_no_pre} of the checked candidates had NO same-orbit pre-event scene within "
                  f"{PRE_EVENT_LOOKBACK_DAYS} days and were skipped", flush=True)
            for r in results[tier_name]:
                print(f"    [{tier_name}] {r['date']}  orbit={r['orbit']}  rain_0_24h={r['rain_0_24h_mm']}mm  rain_24_48h={r['rain_24_48h_mm']}mm", flush=True)

        all_results[city] = results
        # write incrementally so partial progress survives any later failure
        with open(OUT_PATH, "w", encoding="utf-8") as f:
            json.dump(all_results, f, indent=2)

    print(f"\n{'='*100}\nDone. Results written to {OUT_PATH}\n{'='*100}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
