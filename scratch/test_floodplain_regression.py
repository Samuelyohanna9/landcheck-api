"""Regression check for app/utils/hazard_floodplain.py's compute_floodplain_risk() - the new V2
Floodplain Susceptibility engine (HAND / Height Above Nearest Drainage).

Pins the seven real-parcel-diagnostic locations used to DESIGN this engine. These are TRAINING/
DESIGN DATA, not validation evidence - they are exactly the sites whose HAND readings motivated
the 1 - HAND/25 transformation, so an engine that reproduces them well proves nothing about
real-world discrimination on unseen sites. Per the explicit decision to retire this set from any
future validation claim, this fixture exists only to catch UNINTENTIONAL drift in the engine's own
scoring logic (a dependency upgrade, an accidental constant edit, a MERIT Hydro asset change) - not
to serve as evidence the methodology works. Formal blind validation on a fresh, never-before-used
location set is Phase 3, a separate and later piece of work.

The 25m linear-decay constant is a documented, provisional pre-validation modelling assumption
(see compute_floodplain_risk's docstring) - explicitly NOT modified after Phase 3 begins, whatever
Phase 3 shows. Do NOT edit the expected values here to make a future run "pass" for the same reason
test_pluvial_regression.py's docstring states: a legitimate methodology change needs its own
sign-off, not a fixture update.

Run: python scratch/test_floodplain_regression.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath("."))
from dotenv import load_dotenv
load_dotenv()

from app.db import SessionLocal
from app.utils.hazard_floodplain import compute_floodplain_risk

# (name, lat, lon, expected_risk_class, expected_risk_value, tolerance)
FIXTURES = [
    ("Ogunpa River corridor (Ibadan)", 7.3775, 3.8880, "High", 0.572, 0.05),
    ("Bodija GRA (Ibadan)", 7.4270, 3.9080, "Low", 0.184, 0.05),
    ("Makurdi (River Benue)", 7.7322, 8.5391, "High", 0.664, 0.05),
    ("Abuja - Asokoro/Maitama", 9.0479, 7.5289, "Low", 0.072, 0.05),
    ("Ogbaru (River Niger, Anambra)", 6.1667, 6.7500, "Severe", 0.936, 0.05),
    ("Lokoja (Niger-Benue confluence)", 7.8023, 6.7333, "Low", 0.000, 0.05),
    ("Jos (Plateau highland)", 9.8965, 8.8583, "Low", 0.000, 0.05),
]


def _square_boundary(lat: float, lon: float, half_width_deg: float = 0.00027) -> dict:
    d = half_width_deg
    return {
        "type": "Polygon",
        "coordinates": [[
            [lon - d, lat - d], [lon + d, lat - d], [lon + d, lat + d], [lon - d, lat + d], [lon - d, lat - d],
        ]],
    }


def main() -> int:
    db = SessionLocal()
    failures = []
    try:
        for name, lat, lon, expected_class, expected_value, tolerance in FIXTURES:
            boundary = _square_boundary(lat, lon)
            try:
                risk_value, risk_class, breakdown, _png = compute_floodplain_risk(db, boundary)
            except Exception as exc:
                failures.append(f"{name}: raised {exc!r}")
                print(f"[ERROR] {name}: {exc!r}")
                continue

            assert breakdown.get("data_available") is True, (
                f"{name}: data_available was False - MERIT Hydro should have complete global "
                "coverage, unlike GloFAS this is not expected to ever legitimately be missing"
            )
            hand_median = breakdown.get("hand_median_m")
            assert hand_median is not None and hand_median >= 0.0, (
                f"{name}: hand_median_m missing or negative: {hand_median!r}"
            )

            class_ok = risk_class == expected_class
            value_ok = abs(risk_value - expected_value) <= tolerance
            status = "PASS" if (class_ok and value_ok) else "FAIL"
            print(
                f"[{status}] {name}: risk_value={risk_value:.4f} (expected {expected_value:.3f} "
                f"+/-{tolerance}), risk_class={risk_class} (expected {expected_class}), "
                f"hand_median={hand_median}m"
            )
            if status == "FAIL":
                failures.append(
                    f"{name}: got risk_value={risk_value:.4f}/risk_class={risk_class}, "
                    f"expected risk_value~{expected_value:.3f}/risk_class={expected_class}"
                )
    finally:
        db.close()

    print()
    if failures:
        print(f"{len(failures)} of {len(FIXTURES)} fixture(s) FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"All {len(FIXTURES)} fixtures passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
