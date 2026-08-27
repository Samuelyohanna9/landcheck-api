"""One-time (then periodically-rerun) batch export: precomputes the CHIRPS 99th-percentile daily
rainfall ("worst realistic rain day") as a stored Earth Engine asset covering Nigeria, so
hazard_pluvial.py's live per-request code can read one pixel value instead of recomputing a
percentile over the full 16,600+-image CHIRPS DAILY collection on every request - confirmed live
(scratch/chirps_p99_timing_diagnostic.py) to throw ee.ee_exception.EEException: "Computation timed
out" at every window size tested (full record, 30-year, 15-year), because Reducer.percentile over
an ImageCollection has to sort the full per-pixel stack and blows Earth Engine's ~5-minute
interactive compute budget regardless of collection size in this range.

A batch Export task has a much larger compute budget than an interactive getInfo() call, which is
what makes this approach viable at all - same 44-year definition, computed once instead of live.

Run this again whenever the rainfall record should be refreshed (e.g. yearly) - it's cheap and fast
because the region is small (Nigeria at CHIRPS' ~5.5km resolution is a tiny image).
"""
from __future__ import annotations

import time

from dotenv import load_dotenv

load_dotenv()

import ee

from app.utils.gee_client import init_gee

init_gee()

GEE_PROJECT_ID = "landcheck-gee"
ASSET_ID = f"projects/{GEE_PROJECT_ID}/assets/chirps_p99_daily_nigeria"
CHIRPS_ASSET = "UCSB-CHG/CHIRPS/DAILY"
CHIRPS_SCALE_M = 5500

# Nigeria's real extent is ~2.6-14.7 lon, 4.2-13.9 lat - buffered out generously so a plot right at
# the border (or a future near-border expansion) still has real coverage rather than an edge NoData
# pixel.
NIGERIA_BBOX = ee.Geometry.Rectangle([1.5, 3.0, 15.5, 15.0])

chirps_daily = ee.ImageCollection(CHIRPS_ASSET).select("precipitation")
p99_image = (
    chirps_daily.reduce(ee.Reducer.percentile([99]))
    .rename("p99_daily_mm")
    .clip(NIGERIA_BBOX)
    .set({
        "description": "CHIRPS 1981-present daily rainfall, 99th percentile per pixel",
        "source_collection": CHIRPS_ASSET,
        "generated_by": "scratch/export_chirps_p99_nigeria.py",
    })
)

task = ee.batch.Export.image.toAsset(
    image=p99_image,
    description="chirps_p99_daily_nigeria",
    assetId=ASSET_ID,
    region=NIGERIA_BBOX,
    scale=CHIRPS_SCALE_M,
    maxPixels=int(1e9),
)
task.start()
print(f"Started export task: {task.id} -> {ASSET_ID}")

# Poll for a bit so this script's own exit code reflects success/failure when run interactively -
# the task itself keeps running server-side even if this process is killed or times out.
for _ in range(40):
    status = task.status()
    state = status.get("state")
    print(f"  status: {state}")
    if state in ("COMPLETED", "FAILED", "CANCELLED"):
        if state != "COMPLETED":
            raise SystemExit(f"Export did not complete: {status}")
        print(f"Done. Asset ready at: {ASSET_ID}")
        break
    time.sleep(10)
else:
    print("Still running after ~400s - check status separately; the task is not lost.")
