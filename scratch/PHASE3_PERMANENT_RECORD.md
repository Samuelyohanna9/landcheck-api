# Phase 3 Blind Validation — Permanent Record (Failed)

**DO NOT MODIFY OR DELETE these files. This is a permanent record of a failed blind validation of
frozen V2, preserved per explicit instruction. V2 must never be described as validated on the
strength of this result.**

## Preserved artifacts

- `phase3_locations.py` — the locked 20-location candidate set (10 flooded, 10 control) with
  evidence source, event date, and spatial-precision disclosure for every entry. Written and
  locked before any prediction was run.
- `phase3_predictions.json` — the complete raw output of frozen, unmodified V2
  (`_compute_combined_flood`) for all 20 locations. Generated **2026-08-24T20:37:54.273991+00:00**.
  **SHA256: `1d29bd81cc963eb1d59f022647ea723e6811df1420cad0a2575a9e07551d6e5`**
  (recompute with `sha256sum scratch/phase3_predictions.json` or Python's `hashlib.sha256` to
  verify this file has not been altered since generation.)
- `phase3_analysis.py` — the evaluation script that joined labels to the locked predictions and
  computed the metrics below. Re-running it against the unmodified predictions file must reproduce
  identical numbers.

## Result summary (frozen V2, exactly as obtained)

| Metric | Value |
|---|---|
| Overall AUC | 0.530 |
| River AUC | 0.550 (near-meaningless — only 1/20 locations had GloFAS coverage) |
| Floodplain AUC | 0.550 |
| Pluvial AUC | 0.200 |
| River coverage rate | 5% (1/20) |
| Sensitivity | 0.800 |
| **Specificity** | **0.000 — every control was flagged Moderate+** |
| Floodplain-vs-overall correlation | r=0.931 (HAND is effectively driving the whole model) |

**Verdict: frozen V2 failed this blind validation.** Not tuned in response. See
`v3_diagnostic_hand_false_positives.py` and the physical-mechanism investigation that followed
(reported to the user directly, not re-duplicated here) for the follow-up diagnostic work — that
work explicitly reuses these 20 locations as **development/diagnostic data only**, never again as
blind validation for any future V3.

## Status

Awaiting a V3 redesign decision. No production code (`hazard_flood.py`, `hazard_floodplain.py`,
`hazard_pluvial.py`, the router, PDF, or frontend) has been modified as a result of this finding.
