"""Regression check for app/utils/hazard_pluvial.py's compute_pluvial_risk().

Pins the six real-world validation results recorded when the pluvial engine's scoring
methodology (weights, CHIRPS 99th-percentile design storm, classification thresholds) was
frozen. These sites were deliberately chosen to be PURE pluvial/terrain comparisons - not proof
that the engine predicts any specific historical flood event (Lokoja/Maiduguri both involve real
fluvial/dam-failure mechanisms the separate river engine, not this one, is responsible for) - the
point of this fixture is only to catch UNINTENTIONAL drift in the pluvial engine's own scoring
logic (a dependency upgrade, an accidental weight/threshold edit, a data-source outage silently
changing behavior), not to re-litigate whether the methodology itself is "correct".

Do NOT edit the expected values here to make a future run "pass" - if a code change legitimately
alters these numbers, that's a deliberate methodology change that needs its own sign-off, not a
fixture update. If EarthEngine's underlying datasets are ever updated upstream (e.g. Esri
publishes a new Land Cover epoch, CHIRPS backfills a new year), a small drift in the exact
percentages is expected and the tolerance below already allows for it - only a class-tier
(High/Moderate/Low) change or a wildly different score should fail this.

Run: python scratch/test_pluvial_regression.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath("."))
from dotenv import load_dotenv
load_dotenv()

from app.db import SessionLocal
from app.utils.hazard_pluvial import compute_pluvial_risk

# (name, lat, lon, expected_risk_class, expected_risk_value, tolerance)
# expected_risk_value/tolerance let real-world scores drift a little (data updates, floating-point
# differences) without failing the check - expected_risk_class must match exactly.
FIXTURES = [
    ("Lokoja (Niger-Benue confluence)", 7.8023, 6.7333, "High", 0.616, 0.05),
    ("Port Harcourt (Niger Delta)", 4.8156, 7.0498, "High", 0.500, 0.05),
    ("Maiduguri (2024 flood area)", 11.8333, 13.1500, "High", 0.730, 0.05),
    ("Obudu Plateau (rural highland)", 6.7500, 9.3500, "Low", 0.164, 0.05),
    ("Mambilla Plateau (rural highland)", 6.9500, 11.3500, "Moderate", 0.269, 0.05),
    # Malam Madori LGA center - the site that originally prompted this whole investigation
    # (proxy for the August 2026 Jigawa flood event, not a precise match for the actual flooded
    # location). Its Low result is a genuine, disclosed methodology limitation, not a bug: the
    # CHIRPS 99th-percentile design storm here (23.8mm) is a typical extreme for this semi-arid
    # region, not a worst-case-ever figure - see compute_pluvial_risk's docstring. Deliberately
    # preserved here as-is, not tuned toward a "High" result.
    ("Malam Madori LGA center (Jigawa)", 12.5298, 9.8960, "Low", 0.156, 0.05),
]


def _square_boundary(lat: float, lon: float, half_width_deg: float = 0.00035) -> dict:
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
                risk_value, risk_class, breakdown, _png = compute_pluvial_risk(db, boundary)
            except Exception as exc:
                failures.append(f"{name}: raised {exc!r}")
                print(f"[ERROR] {name}: {exc!r}")
                continue

            # The impervious fraction feeding impervious_score must always be a real 0-1 fraction -
            # this is the exact invariant a real bug violated (a fractional-vs-integer pixel-count
            # truncation mismatch let it exceed 1.0, silently inflating every site's score toward
            # "High" regardless of actual land cover). Checked directly here so a regression in
            # this specific invariant fails loudly rather than only showing up as a score drift.
            impervious_score = breakdown.get("impervious_score")
            impervious_pct = breakdown.get("impervious_fraction_pct")
            assert impervious_score is not None and 0.0 <= impervious_score <= 1.0, (
                f"{name}: impervious_score out of [0, 1] range: {impervious_score!r}"
            )
            assert impervious_pct is not None and 0.0 <= impervious_pct <= 100.0, (
                f"{name}: impervious_fraction_pct out of [0, 100] range: {impervious_pct!r}"
            )

            class_ok = risk_class == expected_class
            value_ok = abs(risk_value - expected_value) <= tolerance
            status = "PASS" if (class_ok and value_ok) else "FAIL"
            print(
                f"[{status}] {name}: risk_value={risk_value:.4f} (expected {expected_value:.3f} "
                f"+/-{tolerance}), risk_class={risk_class} (expected {expected_class}), "
                f"impervious={impervious_pct:.1f}%"
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
