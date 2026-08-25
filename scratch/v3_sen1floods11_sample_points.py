"""Pluvial V3 R&D - independent replication test STEP 1: sample candidate flooded/non-flooded
points from Sen1Floods11's hand-labeled chips (Bonafilia et al. 2020, github.com/cloudtostreet/
Sen1Floods11, public GCS bucket gs://sen1floods11, CC license per repo). NO production code touched,
NO model fitted. This is a completely independent dataset from UFO - different authors, different
imagery source (Sentinel-1/2, not PlanetScope), different labelling methodology - used here purely
to test whether the FROZEN V3.1 index (HAND + relative elevation, weights/normalization fixed from
UFO) still discriminates, per explicit instruction not to re-derive anything from this data.

11 hand-labeled events, mechanism established by real per-event research (Sen1Floods11's own
metadata has no driver/mechanism field, unlike UFO's ufo:flood_driver - see the per-event notes
below, each backed by an actual source found this session, not inferred from rainfall alone):
  Bolivia (2018-02):   Fluvial - rivers overflowing (Beni, multiple named rivers), El Nino rains.
  Mekong/Cambodia(08): Fluvial - Lower Mekong Basin extreme rainfall, basin-wide river overflow.
  Ghana (2018-09):     Fluvial/dam-related - Bagre Dam (Burkina Faso) release + river overflow.
  India/Bihar (2016):  Fluvial - Ganga/Ghaghra/Mahananda/Bagmati rivers above danger mark.
  Nigeria (2018-09):   Fluvial/dam-related - Niger/Benue river flooding, Lagdo Dam concerns
                        (Kogi/Niger/Anambra/Delta states) - the well-known 2018 Niger-Benue flood.
  Pakistan (2017-06):  UNKNOWN - monsoon rainfall damage across 4 provinces, no river- or
                        drainage-specific mechanism found this session; excluded from both the
                        fluvial and pluvial buckets rather than guessed.
  Paraguay (2018-10):  Fluvial - explicit Paraguay River overflow at Asuncion (5.61m, above
                        critical level).
  Somalia (2018-05):   Fluvial - Shabelle/Juba river overflow, >50yr return-period river flood.
  Spain (2019-09):     Pluvial - DANA flash-flood event, Alicante ~300mm rain in 2 days, the one
                        clearly pluvial/flash-flood-mechanism event in this dataset.
  Sri Lanka (2017-05): Mixed/fluvial-leaning - SW monsoon + Cyclone Mora precursor, documented
                        Kalu River overflow AND landslides/flash flooding - kept out of the
                        pluvial-only bucket since the river-overflow component is well documented
                        and dominant in sources found, but flagged as genuinely mixed.
  USA (2019-05):       Fluvial - 2019 Midwest floods, Missouri/Mississippi basin, snowmelt+rain.

Net: only Spain is confidently pluvial-mechanism in this dataset - far too thin (n=1) for a
pluvial-only aggregate statistic. This is reported honestly rather than forced into a same-shape
table as UFO's 4-event pluvial subset.

Same JRC Global Surface Water permanent-water exclusion as UFO (applied in step 2, GEE-side) -
Sen1Floods11 ships its own JRCWaterHand layer per chip, but JRC GSW is queried live via GEE here
instead, for exact methodological parity with the UFO run (same asset, same threshold).

2 chips per event selected by |water_frac-0.5| (computed locally from the already-downloaded
LabelHand rasters - no STAC-embedded stats here, unlike UFO).

Run: python scratch/v3_sen1floods11_sample_points.py
"""
import glob
import json
import os

import numpy as np
import rasterio

LABELHAND_DIR = os.path.join(
    "C:/Users/User/AppData/Local/Temp/claude/c--Users-User-Desktop-project/989c6038-a552-4810-95c3-e5fa30239b8a/scratchpad/Sen1Floods11/LabelHand"
)
EVENT_CHIPS = 2
POS_PER_CHIP = 10
NEG_PER_CHIP = 8
RANDOM_SEED = 42

OUT_PATH = os.path.join(os.path.dirname(__file__), "v3_sen1floods11_candidate_points.json")

MECHANISM = {
    "Bolivia": "Fluvial", "Mekong": "Fluvial", "Ghana": "Fluvial",
    "India": "Fluvial", "Nigeria": "Fluvial", "Pakistan": "Unknown",
    "Paraguay": "Fluvial", "Somalia": "Fluvial", "Spain": "Pluvial",
    "Sri-Lanka": "Mixed/Fluvial-leaning", "USA": "Fluvial",
}


def _sample_chip(base_name, rng):
    label_path = os.path.join(LABELHAND_DIR, f"{base_name}_LabelHand.tif")
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

    # CRS EPSG lookup is unreliable in this environment (local PROJ database version mismatch -
    # same warning seen during the UFO run) - Sen1Floods11's README states all chips are already
    # projected to EPSG:4326/WGS84 at 10m, confirmed empirically above (chip bounds are in
    # degrees, not meters), so raster pixel coords are used directly as lon/lat without a transform.

    points = []
    for label_val, idx_arr, rc_arr in ((1, pos_idx, pos_rc), (0, neg_idx, neg_rc)):
        for i in idx_arr:
            row, col = rc_arr[i]
            lon, lat = rasterio.transform.xy(transform, int(row), int(col))
            points.append({"chip_id": base_name, "label": int(label_val), "lon": lon, "lat": lat})
    return points


def main() -> int:
    files = glob.glob(os.path.join(LABELHAND_DIR, "*_LabelHand.tif"))
    by_event = {}
    for f in files:
        base = os.path.basename(f).replace("_LabelHand.tif", "")
        event = base.split("_")[0]
        with rasterio.open(f) as src:
            arr = src.read(1)
        valid = arr[arr != -1]
        if valid.size == 0:
            continue
        water_frac = float((valid == 1).sum()) / valid.size
        by_event.setdefault(event, []).append((base, water_frac))

    rng = np.random.default_rng(RANDOM_SEED)
    all_points = []
    print(f"{'Event':10s} {'Mechanism':24s} {'chips_used':24s} {'pos':4s} {'neg':4s}")
    for event in sorted(by_event):
        chips = sorted(by_event[event], key=lambda c: abs(c[1] - 0.5))[:EVENT_CHIPS]
        event_points = []
        for base_name, _ in chips:
            pts = _sample_chip(base_name, rng)
            for p in pts:
                p["location_code"] = event
                p["flood_driver"] = MECHANISM.get(event, "Unknown")
            event_points.extend(pts)
        n_pos = sum(1 for p in event_points if p["label"] == 1)
        n_neg = sum(1 for p in event_points if p["label"] == 0)
        print(f"{event:10s} {MECHANISM.get(event,'Unknown'):24s} {','.join(c[0] for c in chips):24s} {n_pos:4d} {n_neg:4d}")
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
