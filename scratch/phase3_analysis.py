"""Phase 3 blind validation - ANALYSIS STEP. Reads the already-locked, already-hashed
scratch/phase3_predictions.json (never modified by this script) and joins it against the
ground-truth labels in scratch/phase3_locations.py (a separate file) to compute performance
metrics. This script runs strictly AFTER predictions were captured and hashed - it cannot have
influenced what compute_floodplain_risk/compute_flood_risk/compute_pluvial_risk produced.

Computes: ROC-AUC (overall + each branch individually, via the rank/Mann-Whitney statistic - the
fraction of (flooded, control) pairs where the flooded site scores higher), a confusion matrix at
the "Moderate or higher" operating threshold (same convention the phase-1 study used), sensitivity/
specificity/FPR/FNR, per-group score distributions, river coverage rate, primary-driver
distribution, and a HAND-dominance check (correlation of each branch with the overall score).

Also reconstructs what the OLD (pre-V2) single-formula pluvial score would have been for these same
20 sites, using ONLY the terrain_score/runoff_score/impervious_fraction_pct values already present
in the LOCKED predictions file (no new Earth Engine calls, no re-running old code - literally just
re-combining already-captured numbers under the documented old formula: 0.35 terrain + 0.35 runoff
+ 0.30 impervious). This is a descriptive comparison only, computed after predictions were already
locked, not a controlled paired experiment against the original V1 study (which used a different
19-site set) - reported with that caveat, not as a claim of statistical superiority.

Run: python scratch/phase3_analysis.py
"""
import hashlib
import json
import math
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from phase3_locations import LOCATIONS

PREDICTIONS_PATH = os.path.join(os.path.dirname(__file__), "phase3_predictions.json")


def _pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return float("nan")
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    denom = math.sqrt(vx * vy)
    return cov / denom if denom else float("nan")


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


CLASS_RANK = {"Low": 0, "Moderate": 1, "High": 2, "Severe": 3, "No Data": -1}


def _predicted_positive(risk_class):
    return CLASS_RANK.get(risk_class, -1) >= 1  # Moderate or higher


def main() -> int:
    with open(PREDICTIONS_PATH, "rb") as f:
        raw_bytes = f.read()
    sha256 = hashlib.sha256(raw_bytes).hexdigest()
    payload = json.loads(raw_bytes)
    predictions = {p["name"]: p for p in payload["predictions"] if "error" not in p}
    print(f"Loaded predictions generated at {payload['generated_at_utc']} (SHA256={sha256})")
    print(f"v2_frozen={payload.get('v2_frozen')}, n_predictions={len(predictions)}\n")

    labels = {l["name"]: l["group"] for l in LOCATIONS}
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

    # --- River coverage rate --------------------------------------------------------------------
    river_available = [j for j in joined if j["river"]["data_available"]]
    print(f"River (GloFAS) coverage rate: {len(river_available)}/{len(joined)} ({100*len(river_available)/len(joined):.0f}%)\n")

    # --- Primary driver distribution -------------------------------------------------------------
    print("Primary driver distribution:")
    for group_name, rows in (("flooded", flooded), ("control", control)):
        counts = {}
        for j in rows:
            d = j["overall"]["primary_driver"]
            counts[d] = counts.get(d, 0) + 1
        print(f"  {group_name}: {counts}")
    print()

    # --- Score distributions per branch/group -----------------------------------------------------
    print("Score distributions (mean / median / min / max):")
    for branch in ("river", "floodplain", "pluvial", "overall"):
        for group_name, rows in (("flooded", flooded), ("control", control)):
            vals = [j[branch]["risk_value"] for j in rows]
            print(
                f"  {branch:10s} {group_name:8s}: mean={statistics.mean(vals):.3f} "
                f"median={statistics.median(vals):.3f} min={min(vals):.3f} max={max(vals):.3f}"
            )
    print()

    # --- AUC per branch + overall ------------------------------------------------------------------
    print("ROC-AUC (rank/Mann-Whitney statistic, 0.5=none / 1.0=perfect):")
    auc_by_branch = {}
    for branch in ("river", "floodplain", "pluvial", "overall"):
        pos = [j[branch]["risk_value"] for j in flooded]
        neg = [j[branch]["risk_value"] for j in control]
        auc = _rank_auc(pos, neg)
        auc_by_branch[branch] = auc
        print(f"  {branch}: AUC={auc:.3f}")
    print()

    # --- HAND-dominance check: correlation of each branch with overall score ----------------------
    print("Correlation of each branch's score with the OVERALL score (HAND-dominance check):")
    overall_vals = [j["overall"]["risk_value"] for j in joined]
    for branch in ("river", "floodplain", "pluvial"):
        branch_vals = [j[branch]["risk_value"] for j in joined]
        r = _pearson(overall_vals, branch_vals)
        print(f"  {branch} vs overall: r={r:.3f}")
    print()

    # --- Confusion matrix at "Moderate or higher" operating threshold -----------------------------
    tp = sum(1 for j in flooded if _predicted_positive(j["overall"]["risk_class"]))
    fn = len(flooded) - tp
    fp = sum(1 for j in control if _predicted_positive(j["overall"]["risk_class"]))
    tn = len(control) - fp
    sensitivity = tp / (tp + fn) if (tp + fn) else float("nan")
    specificity = tn / (tn + fp) if (tn + fp) else float("nan")
    fpr = fp / (fp + tn) if (fp + tn) else float("nan")
    fnr = fn / (fn + tp) if (fn + tp) else float("nan")
    print("Confusion matrix at 'Moderate or higher' operating threshold (overall score):")
    print(f"  TP={tp} FN={fn} FP={fp} TN={tn}")
    print(f"  Sensitivity (TPR) = {sensitivity:.3f}")
    print(f"  Specificity (TNR) = {specificity:.3f}")
    print(f"  False positive rate = {fpr:.3f}")
    print(f"  False negative rate = {fnr:.3f}\n")

    # --- V1 formula reconstruction (descriptive only, from already-locked component values) -------
    print("=== V1 formula reconstruction (descriptive comparison only - NOT a controlled re-run) ===")
    print("Uses ONLY terrain_score/runoff_score/impervious_fraction_pct already captured in the")
    print("locked predictions file, recombined under the pre-V2 formula (0.35 terrain + 0.35 runoff")
    print("+ 0.30 impervious), then combined with river via max() the same way V1's two-engine")
    print("architecture did (V1 had no floodplain engine).\n")
    v1_rows = []
    for j in joined:
        p = j["pluvial"]
        impervious_frac = (p.get("impervious_fraction_pct") or 0.0) / 100.0
        v1_pluvial = (p.get("terrain_score") or 0.0) * 0.35 + (p.get("runoff_score") or 0.0) * 0.35 + impervious_frac * 0.30
        v1_overall = max(j["river"]["risk_value"], v1_pluvial)
        v1_rows.append({"name": j["name"], "group": j["group"], "v1_overall": v1_overall})
    v1_flooded = [r["v1_overall"] for r in v1_rows if r["group"] == "flooded"]
    v1_control = [r["v1_overall"] for r in v1_rows if r["group"] == "control"]
    v1_auc = _rank_auc(v1_flooded, v1_control)
    print(f"  V1-reconstructed AUC on these 20 NEW locations: {v1_auc:.3f}")
    print(f"  V2 overall AUC on the same 20 locations:         {auc_by_branch['overall']:.3f}")
    print(f"  (Historical V1 AUC on the ORIGINAL 19-site study set was 0.659 - a different dataset,")
    print(f"   cited here as background only, not a paired comparison.)")
    print(f"  V1-reconstructed mean score: flooded={statistics.mean(v1_flooded):.3f}, control={statistics.mean(v1_control):.3f}")
    print(f"  V2 overall mean score:       flooded={statistics.mean([j['overall']['risk_value'] for j in flooded]):.3f}, "
          f"control={statistics.mean([j['overall']['risk_value'] for j in control]):.3f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
