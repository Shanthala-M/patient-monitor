# Sensor value generation for the 5 vitals we're tracking.
# Each one has a healthy range, a "critical" value it heads towards
# during the deterioration scenario, and how noisy/jittery it is.

import random
import time

VITAL_CONFIG = {
    "heart_rate": {
        "unit": "bpm",
        "normal_range": (60, 90),
        "critical": 150,          # tachycardia
        "noise_sd": 2.0,
        "default_freq_sec": 2,
    },
    "spo2": {
        "unit": "%",
        "normal_range": (96, 99),
        "critical": 82,           # dangerous hypoxia
        "noise_sd": 0.5,
        "default_freq_sec": 3,
    },
    "respiration_rate": {
        "unit": "breaths/min",
        "normal_range": (12, 18),
        "critical": 32,           # tachypnea
        "noise_sd": 1.0,
        "default_freq_sec": 3,
    },
    "body_temp": {
        "unit": "C",
        "normal_range": (36.5, 37.3),
        "critical": 39.5,         # fever
        "noise_sd": 0.1,
        "default_freq_sec": 5,
    },
    # motion doesn't drift like the others, it's handled separately below
    "motion": {
        "unit": "g",
        "normal_range": (0.0, 0.3),
        "critical": None,
        "noise_sd": 0.05,
        "default_freq_sec": 1,
    },
}


def _clamp(value, lo, hi):
    return max(lo, min(hi, value))


def generate_reading(vital_type: str, elapsed_since_scenario_start: float,
                      scenario: str, deterioration_duration_sec: float):
    """Generate one reading. If scenario is "deteriorating" the value
    drifts from a normal baseline towards the critical value over
    deterioration_duration_sec, otherwise it's just noise around a
    random point in the healthy range."""
    cfg = VITAL_CONFIG[vital_type]
    lo, hi = cfg["normal_range"]
    baseline = random.uniform(lo, hi)

    if scenario == "deteriorating" and cfg["critical"] is not None:
        frac = _clamp(elapsed_since_scenario_start / deterioration_duration_sec, 0.0, 1.0)
        midpoint = (lo + hi) / 2
        target = cfg["critical"]
        drifted = midpoint + frac * (target - midpoint)
        value = drifted + random.gauss(0, cfg["noise_sd"])
    else:
        value = baseline + random.gauss(0, cfg["noise_sd"])

    return round(value, 2)


def generate_motion_reading(scenario: str, force_fall: bool = False):
    """Motion sensor works differently to the others - it's not a
    continuous drift, it's basically background noise until a fall
    is triggered. Returns (accel_g, fall_detected)."""
    cfg = VITAL_CONFIG["motion"]
    lo, hi = cfg["normal_range"]

    fall = force_fall

    if fall:
        # 2.5-4.5g is roughly the impact range used in fall detection studies
        accel = round(random.uniform(2.5, 4.5), 2)
    else:
        accel = round(random.uniform(lo, hi) + random.gauss(0, cfg["noise_sd"]), 2)

    return accel, fall
