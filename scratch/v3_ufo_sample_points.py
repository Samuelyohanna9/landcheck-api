"""Pluvial V3 R&D - UFO diagnostic, STEP 1: sample candidate flooded/non-flooded pixel points from
the UFO benchmark's own label GeoTIFFs (Zenodo 10.5281/zenodo.19698577, CC BY 4.0, 14 independent
global urban flood events, 215 PlanetScope chips, expert/authoritative labels - not our own
self-derived SAR masks). NO production code touched, NO model fitted here - this only produces a
candidate-point JSON; JRC GSW permanent-water filtering and GEE feature extraction happen in
v3_ufo_signal_diagnostic.py (step 2), exactly the same "no-fit diagnostic" as the HAD-FCDR25 script.

UFO's own label documentation states the "surface water" class includes pre-existing permanent/
seasonal water bodies, not just flood-caused water - so more candidate positives than needed are
sampled here (oversampled ~2x) to survive the JRC GSW exclusion in step 2 without running short.

Per event: up to EVENT_CHIPS chips are selected, preferring chips whose water-pixel-fraction (read
directly from the STAC item, no raster I/O needed for selection) is closest to 0.5 - i.e. chips that
actually contain a real flooded/non-flooded boundary, not a degenerate all-water or all-dry tile.
From each selected chip's label raster, POS_PER_CHIP candidate flooded pixels (label==1) and
NEG_PER_CHIP non-flooded pixels (label==0) are randomly sampled and converted from the raster's own
CRS (read directly from the GeoTIFF, not trusted from STAC JSON - EPSG varies per event: most are
4326, HTX is 32615, SPS is 32616) to EPSG:4326 lon/lat.

Run: python scratch/v3_ufo_sample_points.py
"""
import glob
import json
import os
import random

import numpy as np
import rasterio
from pyproj import Transformer

UFO_DIR = os.path.join(
    "C:/Users/User/AppData/Local/Temp/claude/c--Users-User-Desktop-project/989c6038-a552-4810-95c3-e5fa30239b8a/scratchpad/UFO/extracted/ufo-dataset"
)
ITEMS_DIR = os.path.join(UFO_DIR, "stac", "items")
LABELS_DIR = os.path.join(UFO_DIR, "urbanFloodsObservationsPUBLIC", "labels")

EVENT_CHIPS = 2          # chips per event, ranked by |water_frac - 0.5|
POS_PER_CHIP = 10        # oversampled - JRC GSW filtering happens in step 2
NEG_PER_CHIP = 8
RANDOM_SEED = 42

OUT_PATH = os.path.join(os.path.dirname(__file__), "v3_ufo_candidate_points.json")


def _load_events():
    collection = json.load(open(os.path.join(UFO_DIR, "stac", "collection.json"), encoding="utf-8"))
    return {e["location_code"]: e for e in collection["ufo:events"]}


def _load_items_by_event():
    by_event = {}
    for path in glob.glob(os.path.join(ITEMS_DIR, "*.json")):
        item = json.load(open(path, encoding="utf-8"))
        p = item["properties"]
        by_event.setdefault(p["ufo:location_code"], []).append({
            "id": item["id"],
            "water_frac": p["ufo:label_water_pixel_fraction"],
        })
    return by_event


def _sample_chip(chip_id, rng):
    label_path = os.path.join(LABELS_DIR, f"{chip_id}.tif")
    with rasterio.open(label_path) as src:
        arr = src.read(1)
        crs = src.crs
        transform = src.transform

    pos_rc = np.argwhere(arr == 1)
    neg_rc = np.argwhere(arr == 0)
    if len(pos_rc) == 0 or len(neg_rc) == 0:
        return []

    pos_idx = rng.choice(len(pos_rc), size=min(POS_PER_CHIP, len(pos_rc)), replace=False)
    neg_idx = rng.choice(len(neg_rc), size=min(NEG_PER_CHIP, len(neg_rc)), replace=False)

    transformer = None
    if crs.to_epsg() != 4326:
        transformer = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)

    points = []
    for label_val, idx_arr, rc_arr in ((1, pos_idx, pos_rc), (0, neg_idx, neg_rc)):
        for i in idx_arr:
            row, col = rc_arr[i]
            x, y = rasterio.transform.xy(transform, int(row), int(col))
            if transformer is not None:
                lon, lat = transformer.transform(x, y)
            else:
                lon, lat = x, y
            points.append({"chip_id": chip_id, "label": int(label_val), "lon": lon, "lat": lat})
    return points


def main() -> int:
    events = _load_events()
    items_by_event = _load_items_by_event()
    rng = np.random.default_rng(RANDOM_SEED)

    all_points = []
    print(f"{'Event':6s} {'Location':32s} {'Driver':12s} {'chips_used':10s} {'pos':4s} {'neg':4s}")
    for loc_code in sorted(events):
        ev = events[loc_code]
        chips = items_by_event.get(loc_code, [])
        chips_ranked = sorted(chips, key=lambda c: abs(c["water_frac"] - 0.5))
        selected = chips_ranked[:EVENT_CHIPS]

        event_points = []
        for c in selected:
            pts = _sample_chip(c["id"], rng)
            for p in pts:
                p["location_code"] = loc_code
                p["location"] = ev["location"]
                p["flood_driver"] = ev["flood_driver"]
                p["event_description"] = ev["event_description"]
                p["estimated_flooding_dates"] = ev["estimated_flooding_dates"]
            event_points.extend(pts)

        n_pos = sum(1 for p in event_points if p["label"] == 1)
        n_neg = sum(1 for p in event_points if p["label"] == 0)
        print(f"{loc_code:6s} {ev['location']:32s} {ev['flood_driver']:12s} "
              f"{','.join(c['id'][-4:] for c in selected):10s} {n_pos:4d} {n_neg:4d}")
        all_points.extend(event_points)

    print(f"\nTotal candidate points: {len(all_points)} "
          f"({sum(1 for p in all_points if p['label']==1)} pos / "
          f"{sum(1 for p in all_points if p['label']==0)} neg)")

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(all_points, f, indent=2)
    print(f"Candidate points written to {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
