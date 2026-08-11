from __future__ import annotations

from typing import Tuple

# Shared 4-tier risk scale used by every hazard module (flood, erosion, ...) so a client sees
# one consistent vocabulary and palette across the whole hazard report suite, not a different
# scale per hazard type.
RISK_TIERS = [
    (0.25, "Low", "#22c55e"),
    (0.50, "Moderate", "#f59e0b"),
    (0.75, "High", "#f97316"),
    (1.01, "Severe", "#ef4444"),
]
NO_DATA_COLOR = "#94a3b8"


def classify_risk(value: float, data_available: bool = True) -> Tuple[str, str]:
    """Maps a 0-1 risk score to (label, hex color). data_available=False always returns the
    "No Data" tier regardless of value, since a 0.0 score from missing data must never be
    displayed the same way as a genuine "Low" score.
    """
    if not data_available:
        return "No Data", NO_DATA_COLOR
    safe_value = max(0.0, min(1.0, float(value)))
    for ceiling, label, color in RISK_TIERS:
        if safe_value < ceiling:
            return label, color
    return "Severe", "#ef4444"


def risk_tier_legend() -> list[dict]:
    labels_seen = set()
    legend = []
    for _, label, color in RISK_TIERS:
        if label in labels_seen:
            continue
        labels_seen.add(label)
        legend.append({"label": label, "color": color})
    return legend
