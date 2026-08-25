"""Pluvial V3 R&D - V3.1 frozen terrain-susceptibility index, STEP 3: test whether a tiny number of
PRE-SPECIFIED, equal-weight combinations of relative_elev_1000m + hand_m (+ depression_300m, +
twi_hs as a secondary variant) beat the ~0.684 median single-feature AUC already found, using
leave-one-event-out logic across the 13 usable UFO events. NO production code touched.

This is deliberately NOT model training: no weight is ever fit to labels. Each candidate is a fixed
a-priori equal-weight average of consistently-oriented, min-max-normalized physical variables. The
ONLY thing that varies per held-out fold is the min/max normalization RANGE, and that range is
computed strictly from the OTHER events' raw predictor values (never from labels, never from the
held-out event) - i.e. it adapts to "what range does HAND take on Earth's other flood events", not
"what threshold best separates flood/non-flood." That keeps this a frozen-formula test, consistent
with "don't search dozens of weights until one works - a simple equal-weight terrain index is enough
for the first test."

Candidates (chosen because relative_elev_1000m and hand_m independently cleared the ~0.684 median /
11-12 of 13 events bar in the prior diagnostic; depression_300m was borderline positive; twi_hs is
explicitly a secondary/demoted candidate per instruction; drain_dist_m, slope_deg, impervious_pct
are excluded from the core hypothesis per instruction):
  A. hand_m + relative_elev_1000m                          (the two strongest individual features)
  B. hand_m + relative_elev_1000m + depression_300m          (core hypothesis as stated)
  C. hand_m + relative_elev_1000m + depression_300m + twi_hs (core + secondary TWI variant)

Each raw feature is first sign-oriented (negated if lower values mean more flood-prone, matching
FEATURE_HIGHER_MEANS_FLOOD_PRONE in v3_ufo_signal_diagnostic.py), then min-max scaled to [0,1] using
the held-out fold's training range, then equal-weight averaged. A point is used in a candidate only
if ALL of that candidate's features are present for it (no imputation - keeps this a genuinely
simple index, not a modeling decision in disguise).

Results reported per-event, aggregated across all 13 usable events AND the pluvial-driver subset
(HTX/KTM/NSW/SLC) - same structure as the single-feature diagnostic, for direct comparison.

Run: python scratch/v3_ufo_frozen_index_diagnostic.py
"""
import json
import os

SAMPLES_PATH = os.path.join(os.path.dirname(__file__), "v3_ufo_samples.json")

FEATURE_HIGHER_MEANS_FLOOD_PRONE = {
    "hand_m": False,
    "relative_elev_1000m": False,
    "depression_300m": True,
    "twi_hs": True,
}

CANDIDATES = {
    "A: HAND + RelElev": ["hand_m", "relative_elev_1000m"],
    "B: HAND + RelElev + Depression": ["hand_m", "relative_elev_1000m", "depression_300m"],
    "C: B + TWI (secondary)": ["hand_m", "relative_elev_1000m", "depression_300m", "twi_hs"],
}

PLUVIAL_EVENTS = {"HTX", "KTM", "NSW", "SLC"}


def _oriented(value, feature):
    return value if FEATURE_HIGHER_MEANS_FLOOD_PRONE[feature] else -value


def _rank_auc(pos, neg):
    pairs = concordant = ties = 0
    for p in pos:
        for n in neg:
            pairs += 1
            if p > n:
                concordant += 1
            elif p == n:
                ties += 1
    return (concordant + 0.5 * ties) / pairs if pairs else float("nan")


def _minmax_from_fold(train_samples, features):
    ranges = {}
    for feat in features:
        vals = [_oriented(s[feat], feat) for s in train_samples if s.get(feat) is not None]
        if not vals:
            ranges[feat] = (0.0, 1.0)
            continue
        ranges[feat] = (min(vals), max(vals))
    return ranges


def _index_value(sample, features, ranges):
    parts = []
    for feat in features:
        raw = sample.get(feat)
        if raw is None:
            return None
        lo, hi = ranges[feat]
        oriented = _oriented(raw, feat)
        scaled = (oriented - lo) / (hi - lo) if hi > lo else 0.5
        parts.append(scaled)
    return sum(parts) / len(parts)


def main() -> int:
    with open(SAMPLES_PATH, encoding="utf-8") as f:
        samples = json.load(f)

    events = sorted(set(s["location_code"] for s in samples))
    print(f"Loaded {len(samples)} samples across {len(events)} events: {events}\n")

    for cand_name, features in CANDIDATES.items():
        print(f"\n{'='*100}\nCANDIDATE {cand_name}  (features: {features})\n{'='*100}")
        header = f"{'Event':6s} {'Driver':12s} {'n_pos':6s} {'n_neg':6s} {'index_AUC':10s}"
        print(header)

        per_event_auc = {}
        for held_out in events:
            train = [s for s in samples if s["location_code"] != held_out]
            test = [s for s in samples if s["location_code"] == held_out]
            if not test:
                continue
            ranges = _minmax_from_fold(train, features)

            flooded_idx = []
            non_flooded_idx = []
            for s in test:
                v = _index_value(s, features, ranges)
                if v is None:
                    continue
                (flooded_idx if s["label"] == 1 else non_flooded_idx).append(v)

            driver = test[0]["flood_driver"]
            if len(flooded_idx) < 2 or len(non_flooded_idx) < 2:
                print(f"{held_out:6s} {driver:12s} {len(flooded_idx):<6d} {len(non_flooded_idx):<6d} {'n/a':10s}")
                per_event_auc[held_out] = None
                continue

            auc = _rank_auc(flooded_idx, non_flooded_idx)
            per_event_auc[held_out] = auc
            print(f"{held_out:6s} {driver:12s} {len(flooded_idx):<6d} {len(non_flooded_idx):<6d} {auc:<10.3f}")

        def _summarize(loc_subset, title):
            vals = [per_event_auc[loc] for loc in loc_subset if per_event_auc.get(loc) is not None]
            if not vals:
                print(f"  {title}: no data")
                return
            vals_sorted = sorted(vals)
            n = len(vals_sorted)
            median = vals_sorted[n // 2] if n % 2 else (vals_sorted[n // 2 - 1] + vals_sorted[n // 2]) / 2
            n_correct = sum(1 for v in vals if v > 0.5)
            print(f"  {title}: median AUC={median:.3f}  correct-direction={n_correct}/{n}")

        print()
        _summarize(events, "ALL usable events")
        _summarize([e for e in events if e in PLUVIAL_EVENTS], "PLUVIAL-driver events only")

    print(f"\n{'='*100}\nFOR REFERENCE - single-feature results from the prior diagnostic run "
          f"(v3_ufo_signal_diagnostic.py):\n"
          f"  hand_m:               12/13 correct, median AUC 0.684\n"
          f"  relative_elev_1000m:  11/13 correct, median AUC 0.684\n"
          f"  depression_300m:      12/13 correct, median AUC 0.644\n"
          f"  twi_hs:               10/12 correct, median AUC 0.598\n"
          f"{'='*100}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
