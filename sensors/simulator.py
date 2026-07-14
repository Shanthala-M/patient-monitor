"""
simulator.py

Simulates ALL 5 vital sensors for a single patient. One container = one
patient. Each vital runs on its own thread with its own publish frequency
(satisfies the brief's "configurable frequency/dispatch rate" requirement),
and publishes JSON readings over MQTT to:

    patients/{patient_id}/vitals/{vital_type}

The fog node (day 2) subscribes to these topics.

Configuration is via environment variables (see docker-compose.yml):

    PATIENT_ID                 e.g. "patient-1"                (required)
    MQTT_HOST                  default "mosquitto"
    MQTT_PORT                  default 1883
    SCENARIO                   "stable" | "deteriorating"       (default "stable")
    SCENARIO_START_SEC         seconds after container start before
                                deterioration begins                (default 30)
    DETERIORATION_DURATION_SEC how long the drift to critical takes (default 60)
    HR_FREQ_SEC / SPO2_FREQ_SEC / RESP_FREQ_SEC / TEMP_FREQ_SEC / MOTION_FREQ_SEC
                                per-sensor publish frequency overrides

Ground truth logging: the exact wall-clock moment deterioration begins is
appended to /data/ground_truth.log (a shared volume). This is what the
fog-vs-cloud-only latency experiment (days 6-7) measures alert time against.
"""

import json
import os
import threading
import time
import signal
import sys

import paho.mqtt.client as mqtt

from vitals import VITAL_CONFIG, generate_reading, generate_motion_reading

PATIENT_ID = os.environ.get("PATIENT_ID", "patient-1")
MQTT_HOST = os.environ.get("MQTT_HOST", "mosquitto")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))

SCENARIO = os.environ.get("SCENARIO", "stable")  # "stable" or "deteriorating"
SCENARIO_START_SEC = float(os.environ.get("SCENARIO_START_SEC", "30"))
DETERIORATION_DURATION_SEC = float(os.environ.get("DETERIORATION_DURATION_SEC", "60"))

GROUND_TRUTH_LOG = os.environ.get("GROUND_TRUTH_LOG", "/data/ground_truth.log")

# If set (seconds since container start), a single fall event is triggered
# at exactly this time — independent of SCENARIO. Leave unset for no fall.
FALL_AT_SEC = os.environ.get("FALL_AT_SEC")
FALL_AT_SEC = float(FALL_AT_SEC) if FALL_AT_SEC else None

FREQ_OVERRIDES = {
    "heart_rate": float(os.environ.get("HR_FREQ_SEC", VITAL_CONFIG["heart_rate"]["default_freq_sec"])),
    "spo2": float(os.environ.get("SPO2_FREQ_SEC", VITAL_CONFIG["spo2"]["default_freq_sec"])),
    "respiration_rate": float(os.environ.get("RESP_FREQ_SEC", VITAL_CONFIG["respiration_rate"]["default_freq_sec"])),
    "body_temp": float(os.environ.get("TEMP_FREQ_SEC", VITAL_CONFIG["body_temp"]["default_freq_sec"])),
    "motion": float(os.environ.get("MOTION_FREQ_SEC", VITAL_CONFIG["motion"]["default_freq_sec"])),
}

_shutdown = threading.Event()
_process_start = time.time()
_deterioration_triggered = threading.Event()
_deterioration_start_time = None
_seq_counters = {v: 0 for v in VITAL_CONFIG}
_seq_lock = threading.Lock()
_fall_pending = threading.Event()  # set briefly to fire exactly one fall reading


def log_ground_truth(event: str):
    """Append a timestamped ground-truth event, used later to measure
    detection latency of the fog node vs. a cloud-only baseline."""
    line = json.dumps({
        "patient_id": PATIENT_ID,
        "event": event,
        "timestamp": time.time(),
    })
    try:
        os.makedirs(os.path.dirname(GROUND_TRUTH_LOG), exist_ok=True)
        with open(GROUND_TRUTH_LOG, "a") as f:
            f.write(line + "\n")
    except OSError as e:
        print(f"[{PATIENT_ID}] WARNING: could not write ground truth log: {e}")
    print(f"[{PATIENT_ID}] GROUND TRUTH: {line}")


def next_seq(vital_type: str) -> int:
    with _seq_lock:
        _seq_counters[vital_type] += 1
        return _seq_counters[vital_type]


def scenario_clock_watcher():
    """Waits until SCENARIO_START_SEC has elapsed, then flips the patient
    into 'deteriorating' mode and logs the ground-truth event. Also fires
    a single scheduled fall event (if FALL_AT_SEC is configured), tracked
    separately from the deterioration scenario."""
    global _deterioration_start_time
    deterioration_done = SCENARIO != "deteriorating"
    fall_done = FALL_AT_SEC is None

    while not _shutdown.is_set() and not (deterioration_done and fall_done):
        elapsed = time.time() - _process_start

        if not deterioration_done and elapsed >= SCENARIO_START_SEC:
            _deterioration_start_time = time.time()
            _deterioration_triggered.set()
            log_ground_truth("deterioration_started")
            deterioration_done = True

        if not fall_done and elapsed >= FALL_AT_SEC:
            _fall_pending.set()
            log_ground_truth("fall_triggered")
            fall_done = True

        time.sleep(0.2)


def sensor_loop(client: mqtt.Client, vital_type: str):
    freq = FREQ_OVERRIDES[vital_type]
    topic = f"patients/{PATIENT_ID}/vitals/{vital_type}"

    while not _shutdown.is_set():
        current_scenario = "stable"
        elapsed = 0.0
        if _deterioration_triggered.is_set():
            current_scenario = "deteriorating"
            elapsed = time.time() - _deterioration_start_time

        if vital_type == "motion":
            # consume the one-shot fall trigger, if it just fired
            force_fall = _fall_pending.is_set()
            if force_fall:
                _fall_pending.clear()

            accel, fall = generate_motion_reading(current_scenario, force_fall=force_fall)
            payload = {
                "patient_id": PATIENT_ID,
                "vital_type": "motion",
                "value": accel,
                "unit": VITAL_CONFIG["motion"]["unit"],
                "fall_detected": fall,
                "seq": next_seq(vital_type),
                "timestamp": time.time(),
            }
            if fall:
                log_ground_truth("fall_detected_by_sensor")
        else:
            value = generate_reading(vital_type, elapsed, current_scenario, DETERIORATION_DURATION_SEC)
            payload = {
                "patient_id": PATIENT_ID,
                "vital_type": vital_type,
                "value": value,
                "unit": VITAL_CONFIG[vital_type]["unit"],
                "seq": next_seq(vital_type),
                "timestamp": time.time(),
            }

        client.publish(topic, json.dumps(payload), qos=1)
        _shutdown.wait(freq)


def handle_shutdown(signum, frame):
    print(f"[{PATIENT_ID}] Shutting down...")
    _shutdown.set()


def main():
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    client = mqtt.Client(client_id=f"sim-{PATIENT_ID}", protocol=mqtt.MQTTv311)
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
    client.loop_start()

    print(f"[{PATIENT_ID}] Connected to MQTT {MQTT_HOST}:{MQTT_PORT}, "
          f"scenario={SCENARIO}, start_delay={SCENARIO_START_SEC}s")

    threads = [threading.Thread(target=scenario_clock_watcher, daemon=True)]
    for vital_type in VITAL_CONFIG:
        threads.append(threading.Thread(target=sensor_loop, args=(client, vital_type), daemon=True))

    for t in threads:
        t.start()

    try:
        while not _shutdown.is_set():
            time.sleep(0.5)
    finally:
        client.loop_stop()
        client.disconnect()
        sys.exit(0)


if __name__ == "__main__":
    main()
