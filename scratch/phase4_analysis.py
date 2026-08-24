"""Phase 4 ANALYSIS - joins each locked predictions file to its own independent label set and
reports branch-specific ROC-AUC, sensitivity, specificity, false positives/negatives, score
distributions, and coverage. Runs strictly after both phase4_*_predictions.json files were already
written and hashed by phase4_run_predictions.py - cannot have influenced what the frozen engines
produced.

Run: python scratch/phase4_analysis.py
"""
import hashlib
import json
import math
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from phase4_fluvial_locations import LOCATIONS as FLUVIAL_LOCATIONS
from phase4_pluvial_locations import LOCATIONS as PLUVIAL_LOCATIONS

HERE = os.path.dirname(__file__)
CLASS_RANK = {"Low": 0, "Moderate": 1, "High": 2, "Severe": 3, "No Data": -1}


def _rank_auc(positive_scores, negative_scores):
    pairs = concordant = ties = 0
    for p in positive_scores:
        for n in negative_scores:
            pairs += 1
            if p > n:
                concordant += 1
            elif p == n:
                ties += 1
    return (concordant + 0.5 * ties) / pairs if pairs else float("nan")


def _predicted_positive(risk_class):
    return CLASS_RANK.get(risk_class, -1) >= 1  # Moderate or higher


def _analyze(predictions_file, locations, dataset_label, branches):
    path = os.path.join(HERE, predictions_file)
    with open(path, "rb") as f:
        raw_bytes = f.read()
    sha256 = hashlib.sha256(raw_bytes).hexdigest()
    payload = json.loads(raw_bytes)
    predictions = {p["name"]: p for p in payload["predictions"] if "error" not in p}

    print(f"\n{'='*100}")
    print(f"=== {dataset_label} ===")
    print(f"{'='*100}")
    print(f"Predictions: {predictions_file}, generated {payload['generated_at_utc']}, SHA256={sha256}")

    labels = {l["name"]: l["group"] for l in locations}
    joined = []
    for name, pred in predictions.items():
        group = labels.get(name)
        if group is None:
            print(f"[WARN] {name}: prediction has no matching label - skipped")
            continue
        joined.append({"name": name, "group": group, **pred})

    flooded = [j for j in joined if j["group"] == "flooded"]
    control = [j for j in joined if j["group"] == "control"]
    print(f"Joined: {len(flooded)} flooded, {len(control)} control ({len(joined)} total)\n")

    river_available = [j for j in joined if j["river"]["data_available"]]
    print(f"River (GloFAS) coverage rate: {len(river_available)}/{len(joined)} ({100*len(river_available)/len(joined):.0f}%)\n")

    print("Score distributions (mean / median / min / max):")
    for branch in branches + ["overall"]:
        for group_name, rows in (("flooded", flooded), ("control", control)):
            vals = [j[branch]["risk_value"] for j in rows]
            print(
                f"  {branch:10s} {group_name:8s}: mean={statistics.mean(vals):.3f} "
                f"median={statistics.median(vals):.3f} min={min(vals):.3f} max={max(vals):.3f}"
            )
    print()

    print("ROC-AUC (rank/Mann-Whitney statistic, 0.5=none / 1.0=perfect):")
    for branch in branches + ["overall"]:
        pos = [j[branch]["risk_value"] for j in flooded]
        neg = [j[branch]["risk_value"] for j in control]
        auc = _rank_auc(pos, neg)
        print(f"  {branch}: AUC={auc:.3f}")
    print()

    print("Confusion matrix at 'Moderate or higher' operating threshold, per branch:")
    for branch in branches + ["overall"]:
        tp = sum(1 for j in flooded if _predicted_positive(j[branch]["risk_class"]))
        fn = len(flooded) - tp
        fp = sum(1 for j in control if _predicted_positive(j[branch]["risk_class"]))
        tn = len(control) - fp
        sens = tp / (tp + fn) if (tp + fn) else float("nan")
        spec = tn / (tn + fp) if (tn + fp) else float("nan")
        fpr = fp / (fp + tn) if (fp + tn) else float("nan")
        fnr = fn / (fn + tp) if (fn + tp) else float("nan")
        print(f"  {branch:10s}: TP={tp} FN={fn} FP={fp} TN={tn} | sens={sens:.3f} spec={spec:.3f} FPR={fpr:.3f} FNR={fnr:.3f}")
    print()

    print("Per-site detail:")
    for j in joined:
        print(
            f"  [{j['group']:8s}] {j['name']:38s} overall={j['overall']['risk_value']:.3f}/{j['overall']['risk_class']:10s} "
            f"river={j['river']['risk_value']:.3f}{'*' if not j['river']['data_available'] else ' '} "
            f"floodplain={j['floodplain']['risk_value']:.3f} pluvial={j['pluvial']['risk_value']:.3f}"
        )


def main() -> int:
    _analyze(
        "phase4_fluvial_predictions.json", FLUVIAL_LOCATIONS,
        "FLUVIAL VALIDATION (tests River + Floodplain branches against documented river-flood LGAs)",
        branches=["river", "floodplain", "pluvial"],
    )
    _analyze(
        "phase4_pluvial_predictions.json", PLUVIAL_LOCATIONS,
        "PLUVIAL VALIDATION (tests Pluvial branch against documented urban rainfall/drainage floods)",
        branches=["river", "floodplain", "pluvial"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
