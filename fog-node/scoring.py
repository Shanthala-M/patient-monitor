"""
scoring.py

Composite early-warning score, modeled loosely on hospital early-warning
systems like NEWS2: each vital sign is scored 0-3 based on how far it is
from a healthy range, and the per-vital scores are summed into one number
per patient. Fall detection is handled separately as an automatic alert
(a fall doesn't need points — it's unambiguous).

This is the core piece of "fog logic" the brief asks for: the decision
about whether to alert happens HERE, at the fog node, using only the
patient's own recent readings — no round-trip to the cloud required.
That's the whole justification for using fog computing on this use case.

RISK_THRESHOLD is deliberately a parameter (not hardcoded) so days 6-7 can
run the same pipeline at 2-3 different thresholds and report the
precision/recall trade-off, as required by the brief's experiment plan.
"""

# Score bands per vital: list of (min, max, points), checked in order.
# max is exclusive. Based loosely on NEWS2 scoring bands.
SCORE_BANDS = {
    "heart_rate": [
        (0, 40, 3), (40, 51, 1), (51, 91, 0),
        (91, 111, 1), (111, 131, 2), (131, 999, 3),
    ],
    "spo2": [
        (0, 92, 3), (92, 94, 2), (94, 96, 1), (96, 101, 0),
    ],
    "respiration_rate": [
        (0, 9, 3), (9, 12, 1), (12, 21, 0), (21, 25, 2), (25, 999, 3),
    ],
    "body_temp": [
        (0, 35.1, 3), (35.1, 36.1, 1), (36.1, 38.1, 0),
        (38.1, 39.1, 1), (39.1, 999, 2),
    ],
}


def _score_single_vital(vital_type: str, value: float) -> int:
    for lo, hi, points in SCORE_BANDS[vital_type]:
        if lo <= value < hi:
            return points
    return 0  # shouldn't happen if bands are exhaustive, but fail safe


def compute_score(latest_vitals: dict) -> dict:
    """
    latest_vitals: {"heart_rate": 74.2, "spo2": 97.1, "respiration_rate": 14,
                     "body_temp": 36.8}  (missing vitals are skipped)

    Returns: {"total_score": int, "breakdown": {vital_type: points, ...}}
    """
    breakdown = {}
    for vital_type in SCORE_BANDS:
        if vital_type in latest_vitals:
            breakdown[vital_type] = _score_single_vital(vital_type, latest_vitals[vital_type])

    return {
        "total_score": sum(breakdown.values()),
        "breakdown": breakdown,
    }


def classify(total_score: int, fall_detected: bool, risk_threshold: int) -> str:
    """
    Returns "alert" or "stable".

    A fall is always an immediate alert regardless of score — a collapsed
    patient needs attention even if their vitals happen to look okay at
    that instant. Otherwise, alert only if the composite score has crossed
    risk_threshold (the configurable sensitivity knob for the days 6-7
    precision/recall experiment).
    """
    if fall_detected:
        return "alert"
    if total_score >= risk_threshold:
        return "alert"
    return "stable"
