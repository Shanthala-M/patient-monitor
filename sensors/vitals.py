"""
vitals.py

Defines the 5 simulated sensor types and the math behind their values.

Each vital has:
  - a normal (healthy) range
  - a "critical" target value it drifts towards during a scripted
    deterioration scenario (used later for the fog-vs-cloud latency
    experiment)
  - a default publish frequency (overridable via env vars in simulator.py)

Design note: values are generated as normal-range + gaussian noise while
stable. During deterioration, a linear interpolation moves the baseline
from "normal" towards "critical" over DETERIORATION_DURATION_SEC, with
noise layered on top. This gives reproducible, controllable ground truth
for the experiments in the project brief (days 6-7).
"""

import random
import time

# (low, high) = normal healthy range
# critical = the value the vital drifts towards when scenario="deteriorating"
# noise_sd = standard deviation of gaussian noise added each reading
# default_freq_sec = how often this sensor publishes by default
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
        "critical": 39.5,         # high fever
        "noise_sd": 0.1,
        "default_freq_sec": 5,
    },
    # motion is handled specially (event-based, not continuous drift) —
    # see generate_motion_reading() below
    "motion": {
        "unit": "g",
        "normal_range": (0.0, 0.3),   # background accelerometer noise
        "critical": None,
        "noise_sd": 0.05,
        "default_freq_sec": 1,
    },
}


def _clamp(value, lo, hi):
    return max(lo, min(hi, value))


def generate_reading(vital_type: str, elapsed_since_scenario_start: float,
                      scenario: str, deterioration_duration_sec: float):
    """
    Returns a single simulated reading for a given vital type.

    scenario: "stable" or "deteriorating"
    elapsed_since_scenario_start: seconds since deterioration was triggered
        (ignored if scenario == "stable")
    """
    cfg = VITAL_CONFIG[vital_type]
    lo, hi = cfg["normal_range"]
    baseline = random.uniform(lo, hi)

    if scenario == "deteriorating" and cfg["critical"] is not None:
        # linear interpolation fraction 0 -> 1 over the drift window
        frac = _clamp(elapsed_since_scenario_start / deterioration_duration_sec, 0.0, 1.0)
        midpoint = (lo + hi) / 2
        target = cfg["critical"]
        drifted = midpoint + frac * (target - midpoint)
        value = drifted + random.gauss(0, cfg["noise_sd"])
    else:
        value = baseline + random.gauss(0, cfg["noise_sd"])

    return round(value, 2)


def generate_motion_reading(scenario: str, force_fall: bool = False):
    """
    Motion/fall sensor is event-based rather than a continuous drift.
    Returns (accel_g, fall_detected: bool).

    Falls are NOT randomly generated on every reading — that produced too
    many scattered fall events to get a clean latency measurement. Instead
    a fall only happens when force_fall=True is explicitly passed in by
    the caller (simulator.py triggers this once, at a specific scheduled
    time), giving one sharp, reproducible ground-truth event to measure
    detection latency against.
    """
    cfg = VITAL_CONFIG["motion"]
    lo, hi = cfg["normal_range"]

    fall = force_fall

    if fall:
        # a fall produces a sharp accelerometer spike (2.5g - 4.5g is a
        # commonly cited impact range in fall-detection literature)
        accel = round(random.uniform(2.5, 4.5), 2)
    else:
        accel = round(random.uniform(lo, hi) + random.gauss(0, cfg["noise_sd"]), 2)

    return accel, fall
