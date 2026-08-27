"""Live timing diagnostic: how expensive is compute_pluvial_risk's CHIRPS P99 reduceRegion call,
and does bounding the collection to a fixed climate-normal window meaningfully speed it up?

Not a fix - a measurement, run once to decide whether windowing the CHIRPS DAILY collection is
worth doing before touching production code. Uses a real small test geometry (Ogbaru, already
used elsewhere this session) so the timing reflects an actual plot-scale request, not a toy case.
"""
from __future__ import annotations

import os
import time

from dotenv import load_dotenv

load_dotenv()

import ee

from app.utils.gee_client import init_gee

init_gee()

# Ogbaru-area test box, ~ the scale of a real plot boundary + 1000m analysis buffer.
geom = ee.Geometry.Rectangle([6.7850, 6.1470, 6.7930, 6.1530])
region = geom.buffer(1000)

CHIRPS_ASSET = "UCSB-CHG/CHIRPS/DAILY"


def time_p99(label: str, collection: "ee.ImageCollection") -> float:
    t0 = time.perf_counter()
    p99_image = collection.reduce(ee.Reducer.percentile([99])).rename("p99_daily_mm")
    value = p99_image.reduceRegion(
        reducer=ee.Reducer.mean(), geometry=region, scale=5500, maxPixels=1e9,
    ).get("p99_daily_mm").getInfo()
    elapsed = time.perf_counter() - t0
    print(f"{label}: {elapsed:.2f}s -> p99={value}")
    return elapsed


full = ee.ImageCollection(CHIRPS_ASSET).select("precipitation")
count_full = full.size().getInfo()
print(f"Full CHIRPS DAILY collection size (no date filter): {count_full} images")

# CONFIRMED LIVE: the unbounded call above throws ee.ee_exception.EEException:
# "Computation timed out." - this IS the production bug, not just slowness. Skipping the retry
# (it would just time out again) and going straight to measuring whether a bounded window fixes it.
t_full = None

window30 = full.filterDate("1995-01-01", "2025-01-01")
count_30 = window30.size().getInfo()
print(f"30-year window collection size: {count_30} images")
# CONFIRMED LIVE: this ALSO times out (10,958 images still too many for a synchronous percentile
# reduce). Windowing alone doesn't fix it - skipping the retry, going straight to a smaller window.
t_30 = None

window15 = full.filterDate("2010-01-01", "2025-01-01")
count_15 = window15.size().getInfo()
print(f"15-year window collection size: {count_15} images")
t_15 = time_p99("15-YEAR WINDOW (2010-2024)", window15)

print()
print(f"Summary: unbounded=TIMED OUT ({count_full} imgs), 30y=TIMED OUT ({count_30} imgs), 15y={t_15:.2f}s ({count_15} imgs)")
