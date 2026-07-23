# Early warning score, roughly based on NEWS2 (the score hospitals use
# for spotting deteriorating patients). Each vital gets 0-3 points based
# on how far off it is, points get summed, and if the total crosses
# RISK_THRESHOLD we alert. A fall always alerts regardless of score since
# that's unambiguous on its own.
#
# This is where the actual "fog" decision happens - scoring runs here at
# the edge instead of shipping raw data to the cloud first.

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
    return 0


def compute_score(latest_vitals: dict) -> dict:
    # latest_vitals looks like {"heart_rate": 74.2, "spo2": 97.1, ...}
    # missing vitals just get skipped
    breakdown = {}
    for vital_type in SCORE_BANDS:
        if vital_type in latest_vitals:
            breakdown[vital_type] = _score_single_vital(vital_type, latest_vitals[vital_type])

    return {
        "total_score": sum(breakdown.values()),
        "breakdown": breakdown,
    }


def classify(total_score: int, fall_detected: bool, risk_threshold: int) -> str:
    if fall_detected:
        return "alert"
    if total_score >= risk_threshold:
        return "alert"
    return "stable"
