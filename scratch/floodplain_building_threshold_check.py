"""Live diagnostic: FLOODPLAIN_THREATENED_PCT (hazard_map_renderer.py) flags a building as "on
susceptible ground" whenever its LOCAL interpolated score exceeds a fixed 60%. Given the national
floodplain score distribution runs ~64-81% almost everywhere (see floodplain_tier_recalibration.py),
a flat 60% cutoff should flag nearly every building at nearly every real site - this measures that
directly, at real building-dense sites, across several candidate thresholds, using the actual
production code path (monkeypatching the module constant before each run, not re-deriving the math
separately) so the numbers reflect exactly what a customer would see.
"""
from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()

import app.utils.hazard_map_renderer as hazard_map_renderer
from app.db import SessionLocal
from app.utils.hazard_floodplain import compute_floodplain_risk


def box(lng, lat, d=0.0015):
    return {
        "type": "Polygon",
        "coordinates": [[[lng - d, lat - d], [lng + d, lat - d], [lng + d, lat + d], [lng - d, lat + d], [lng - d, lat - d]]],
    }


SITES = {
    "Ibadan - Bodija (dense urban)": (3.9000, 7.4167),
    "Port Harcourt (dense delta urban)": (7.0134, 4.8156),
    "Lagos mainland - Yaba (dense urban)": (3.3792, 6.5083),
    "Kaduna city": (7.4383, 10.5222),
}

CANDIDATE_THRESHOLDS = [60.0, 70.0, 74.0, 77.0, 80.0, 85.0]

db = SessionLocal()
results = {}
for name, (lng, lat) in SITES.items():
    results[name] = {}
    for threshold in CANDIDATE_THRESHOLDS:
        hazard_map_renderer.FLOODPLAIN_THREATENED_PCT = threshold
        risk_value, risk_class, breakdown, _png = compute_floodplain_risk(db, box(lng, lat))
        total = breakdown.get("buildings_total", 0)
        threatened = breakdown.get("buildings_threatened", 0)
        pct = (threatened / total * 100) if total else 0.0
        results[name][threshold] = (threatened, total, pct)
        print(f"{name:<38} thr={threshold:>5.0f}%  risk_value={risk_value:.3f}  {threatened}/{total} flagged ({pct:.1f}%)")
    print()
db.close()

print("Summary (buildings flagged %):")
header = "Site".ljust(38) + "".join(f"{t:>8.0f}%" for t in CANDIDATE_THRESHOLDS)
print(header)
for name, by_thr in results.items():
    row = name.ljust(38) + "".join(f"{by_thr[t][2]:>8.1f}%" for t in CANDIDATE_THRESHOLDS)
    print(row)
