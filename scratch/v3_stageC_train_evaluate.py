"""Pluvial V3 R&D - STAGE C, step 3: train baseline + tree models on the visually-reviewed,
same-orbit-only observed-flood-mask samples, validate by holding out ENTIRE CITIES (never random
pixels), and preserve every model version + the feature-definition manifest. NO production code
touched. NOTHING here is promoted to production - these numbers are a feasibility check on an
N=48-sample, 3-city pipeline, not a production-grade result, and are reported as such.

Threshold for sensitivity/specificity/FPR/FNR is FIXED at 0.5 for every model/fold - never tuned
against the held-out city, per explicit instruction.

Run: python scratch/v3_stageC_train_evaluate.py
"""
import json
import os
import sys
from datetime import datetime, timezone

import numpy as np
from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    average_precision_score, brier_score_loss, confusion_matrix, roc_auc_score,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

OUT_DIR = os.path.join(os.path.dirname(__file__), "v3_stageC_masks")
SAMPLES_PATH = os.path.join(OUT_DIR, "stageC_samples.json")
FIXED_THRESHOLD = 0.5

HSG_ORDER = {"A": 0, "B": 1, "C": 2, "D": 3}

FEATURE_DEFS = {
    "twi_hs": "TWI = ln(HydroSHEDS-flow-accumulation-derived contributing area / tan(slope)) - Stage A's best-performing raw signal (4/5 matched pairs correct direction).",
    "slope_deg": "Copernicus GLO-30 terrain slope, degrees.",
    "depression_300m": "focal_mean(DEM, 300m) - DEM at point; positive = local basin relative to immediate surroundings (same window as production hazard_pluvial.py).",
    "relative_elev_1000m": "DEM at point - focal_mean(DEM, 1000m); negative = lower than the regional context.",
    "chirps_p99_mm": "CHIRPS 99th-percentile daily rainfall (climatological design storm, same as production).",
    "event_rain_mm": "Actual CHIRPS daily rainfall summed over the event date +/- 1 day - event-specific, NOT in production.",
    "sand_pct": "OpenLandMap surface sand fraction, %.",
    "clay_pct": "OpenLandMap surface clay fraction, %.",
    "hsg_ordinal": "Hydrologic Soil Group (A=0..D=3, derived from sand/clay) - ordinal-encoded for these models.",
    "runoff_mm": "SCS-CN runoff depth using event_rain_mm (not climatological) + derived HSG + default site type.",
    "runoff_coefficient": "SCS-CN runoff coefficient, same inputs.",
    "impervious_pct": "Esri 10m LULC built-fraction, parcel scale (30m buffer) - same source as production, contextual only per the V2 double-count-fix lesson.",
    "drain_dist_m": "Distance to nearest HydroSHEDS local drainage channel (flow-acc>100), capped 2000m - same reliability caveat as production (search-radius artifact beyond the cap).",
    "hand_m": "MERIT Hydro Height Above Nearest Drainage - included ONLY as an optional contextual predictor per instruction; its feature-importance/coefficient weight is explicitly reported below to check for dominance.",
}
ALL_FEATURES = ["twi_hs", "slope_deg", "depression_300m", "relative_elev_1000m", "chirps_p99_mm",
                 "event_rain_mm", "sand_pct", "clay_pct", "hsg_ordinal", "runoff_mm",
                 "runoff_coefficient", "impervious_pct", "drain_dist_m", "hand_m"]
BASELINE_TWI_ONLY = ["twi_hs"]
BASELINE_TWI_RELELEV = ["twi_hs", "relative_elev_1000m"]


def _load_samples():
    with open(SAMPLES_PATH, encoding="utf-8") as f:
        samples = json.load(f)
    for s in samples:
        s["hsg_ordinal"] = HSG_ORDER.get(s.get("hsg"), 1)
    return samples


def _matrix(samples, feature_names, medians=None):
    if medians is None:
        medians = {}
        for name in feature_names:
            vals = [s[name] for s in samples if s.get(name) is not None]
            medians[name] = float(np.median(vals)) if vals else 0.0
    X = np.array([[s[name] if s.get(name) is not None else medians[name] for name in feature_names] for s in samples], dtype=float)
    y = np.array([s["label"] for s in samples], dtype=int)
    return X, y, medians


def _metrics(y_true, y_prob, y_pred):
    tn, fp, fn, tp = confusion_matrix(y_true, y_pred, labels=[0, 1]).ravel()
    sens = tp / (tp + fn) if (tp + fn) else float("nan")
    spec = tn / (tn + fp) if (tn + fp) else float("nan")
    fpr = fp / (fp + tn) if (fp + tn) else float("nan")
    fnr = fn / (fn + tp) if (fn + tp) else float("nan")
    try:
        auc = roc_auc_score(y_true, y_prob) if len(set(y_true)) > 1 else float("nan")
    except ValueError:
        auc = float("nan")
    try:
        pr_auc = average_precision_score(y_true, y_prob) if len(set(y_true)) > 1 else float("nan")
    except ValueError:
        pr_auc = float("nan")
    try:
        brier = brier_score_loss(y_true, y_prob)
    except ValueError:
        brier = float("nan")
    return {"auc": auc, "pr_auc": pr_auc, "sensitivity": sens, "specificity": spec, "fpr": fpr, "fnr": fnr, "brier": brier, "n": len(y_true)}


def _make_model(kind):
    if kind == "logistic":
        return Pipeline([("scale", StandardScaler()), ("clf", LogisticRegression(max_iter=1000))])
    if kind == "random_forest":
        return RandomForestClassifier(n_estimators=200, max_depth=4, min_samples_leaf=2, random_state=42)
    if kind == "gradient_boosted":
        return GradientBoostingClassifier(n_estimators=100, max_depth=2, learning_rate=0.1, random_state=42)
    raise ValueError(kind)


def main() -> int:
    samples = _load_samples()
    cities = sorted(set(s["city"] for s in samples))
    print(f"Loaded {len(samples)} samples across {len(cities)} cities: {cities}\n")
    for city in cities:
        n = sum(1 for s in samples if s["city"] == city)
        n_pos = sum(1 for s in samples if s["city"] == city and s["label"] == 1)
        conf = next(s["mask_confidence"] for s in samples if s["city"] == city)
        print(f"  {city}: n={n} (positive={n_pos}) mask_confidence={conf}")

    model_specs = [
        ("TWI-only (logistic)", "logistic", BASELINE_TWI_ONLY),
        ("TWI+relative-elevation (logistic)", "logistic", BASELINE_TWI_RELELEV),
        ("Full-feature logistic regression", "logistic", ALL_FEATURES),
        ("Random Forest", "random_forest", ALL_FEATURES),
        ("Gradient Boosted Trees", "gradient_boosted", ALL_FEATURES),
    ]

    print(f"\n{'='*100}\nLEAVE-ONE-CITY-OUT CROSS-VALIDATION (fixed threshold={FIXED_THRESHOLD}, never tuned on held-out city)\n{'='*100}")
    all_results = {}
    for label, kind, features in model_specs:
        print(f"\n--- {label} ---")
        per_city = {}
        pooled_true, pooled_prob = [], []
        for held_out in cities:
            train_samples = [s for s in samples if s["city"] != held_out]
            test_samples = [s for s in samples if s["city"] == held_out]
            X_train, y_train, medians = _matrix(train_samples, features)
            X_test, y_test, _ = _matrix(test_samples, features, medians=medians)
            if len(set(y_train)) < 2:
                print(f"  [{held_out}] SKIPPED - training fold has only one class present")
                continue
            model = _make_model(kind)
            model.fit(X_train, y_train)
            y_prob = model.predict_proba(X_test)[:, 1]
            y_pred = (y_prob >= FIXED_THRESHOLD).astype(int)
            m = _metrics(y_test, y_prob, y_pred)
            per_city[held_out] = m
            pooled_true.extend(y_test.tolist())
            pooled_prob.extend(y_prob.tolist())
            print(f"  held-out={held_out:12s} n={m['n']:2d} AUC={m['auc']:.3f} PR-AUC={m['pr_auc']:.3f} "
                  f"sens={m['sensitivity']:.3f} spec={m['specificity']:.3f} FPR={m['fpr']:.3f} FNR={m['fnr']:.3f} brier={m['brier']:.3f}")
        pooled_pred = [1 if p >= FIXED_THRESHOLD else 0 for p in pooled_prob]
        pooled_m = _metrics(np.array(pooled_true), np.array(pooled_prob), np.array(pooled_pred)) if pooled_true else None
        if pooled_m:
            print(f"  POOLED (all held-out folds combined): AUC={pooled_m['auc']:.3f} PR-AUC={pooled_m['pr_auc']:.3f} "
                  f"sens={pooled_m['sensitivity']:.3f} spec={pooled_m['specificity']:.3f} brier={pooled_m['brier']:.3f}")
        all_results[label] = {"per_city": per_city, "pooled": pooled_m}

    # --- Fit final full-data models (all 3 cities) for feature-importance/coefficient inspection
    # and for preservation - NOT used for any of the held-out metrics above (those are strictly
    # per-fold), only to check whether HAND dominates as instructed.
    print(f"\n{'='*100}\nFULL-DATA FEATURE IMPORTANCE / COEFFICIENTS (all 3 cities, for HAND-dominance check only)\n{'='*100}")
    X_all, y_all, _ = _matrix(samples, ALL_FEATURES)
    os.makedirs(OUT_DIR, exist_ok=True)
    preserved = {}
    for kind, name in [("logistic", "logistic_full"), ("random_forest", "random_forest_full"), ("gradient_boosted", "gbm_full")]:
        model = _make_model(kind)
        model.fit(X_all, y_all)
        if kind == "logistic":
            coefs = model.named_steps["clf"].coef_[0]
            importance = dict(zip(ALL_FEATURES, [round(float(c), 3) for c in coefs]))
        else:
            importance = dict(zip(ALL_FEATURES, [round(float(v), 3) for v in model.feature_importances_]))
        ranked = sorted(importance.items(), key=lambda kv: abs(kv[1]), reverse=True)
        hand_rank = [i for i, (k, _) in enumerate(ranked) if k == "hand_m"][0] + 1
        print(f"\n  {name}: top features = {ranked[:5]}")
        print(f"  hand_m rank: {hand_rank} of {len(ALL_FEATURES)} ({'DOMINANT - flag' if hand_rank <= 2 else 'not dominant'})")
        import joblib
        model_path = os.path.join(OUT_DIR, f"{name}.joblib")
        joblib.dump(model, model_path)
        preserved[name] = {"path": model_path, "feature_importance_or_coef": importance, "hand_rank": hand_rank}

    manifest = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "n_samples": len(samples), "cities": cities,
        "feature_definitions": FEATURE_DEFS,
        "fixed_threshold": FIXED_THRESHOLD,
        "leave_one_city_out_results": {
            label: {
                "per_city": {c: m for c, m in res["per_city"].items()},
                "pooled": res["pooled"],
            } for label, res in all_results.items()
        },
        "preserved_models": preserved,
        "not_promoted_to_production": True,
    }
    manifest_path = os.path.join(OUT_DIR, "stageC_model_manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, default=str)
    print(f"\nManifest + models preserved in {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
