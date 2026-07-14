"""
fog_node.py

This is the fog layer: a single process that sits "close to the patients"
(in the real deployment this would run on a local gateway; here it's just
another container on the same Docker network as the sensors) and makes
the alert/no-alert DECISION locally, instead of shipping raw vitals to the
cloud for every single reading.

What it does:
  1. Subscribes to patients/+/vitals/+ on MQTT (every sensor, every patient)
  2. Keeps the latest reading of each vital per patient in memory
  3. On every new reading, recomputes that patient's composite early-warning
     score (scoring.py) and checks for a fall
  4. If score >= RISK_THRESHOLD or a fall was just detected -> publishes an
     IMMEDIATE ALERT to fog/{patient_id}/alert
  5. Otherwise, a background thread publishes a PERIODIC SUMMARY every
     SUMMARY_INTERVAL_SEC to fog/{patient_id}/summary (this is the "only
     bother the cloud occasionally when nothing's wrong" behaviour that
     is the whole point of doing scoring at the edge)

For now this publishes back to the same local Mosquitto broker, on new
topics (fog/...). Days 3-4 swap these publish calls for AWS IoT Core /
forward them into the SQS -> Lambda -> DynamoDB chain — the decision
logic here doesn't need to change at all.

Alert latency logging: every alert is timestamped and printed/logged, so
you can diff it against shared-data/ground_truth.log to get the fog node's
detection latency — the headline number for the days 6-7 experiment.
"""

import json
import os
import threading
import time

import paho.mqtt.client as mqtt

from scoring import compute_score, classify
import aws_publisher

MQTT_HOST = os.environ.get("MQTT_HOST", "mosquitto")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))

# The sensitivity knob for the days 6-7 experiment: lower = more sensitive
# (catches problems earlier, more false alarms). Try 3, 5, 7 and compare.
RISK_THRESHOLD = int(os.environ.get("RISK_THRESHOLD", "5"))

# How often a stable patient still gets a "heartbeat" summary sent, even
# though nothing is wrong (proves the patient/link is still alive).
SUMMARY_INTERVAL_SEC = float(os.environ.get("SUMMARY_INTERVAL_SEC", "15"))

# Don't re-fire an alert every single reading while score stays high —
# only re-alert after this many seconds of continued high risk.
ALERT_COOLDOWN_SEC = float(os.environ.get("ALERT_COOLDOWN_SEC", "20"))

ALERT_LOG = os.environ.get("ALERT_LOG", "/data/fog_alerts.log")

# ---- shared state (protected by _state_lock) --------------------------
_state_lock = threading.Lock()
_patients = {}
# _patients[patient_id] = {
#     "vitals": {"heart_rate": 74.2, "spo2": 97.1, ...},
#     "fall_detected": False,
#     "last_alert_time": 0.0,
# }

_shutdown = threading.Event()


def _get_patient(patient_id: str) -> dict:
    if patient_id not in _patients:
        _patients[patient_id] = {"vitals": {}, "fall_detected": False, "last_alert_time": 0.0}
    return _patients[patient_id]


def log_event(kind: str, patient_id: str, extra: dict):
    record = {"kind": kind, "patient_id": patient_id, "timestamp": time.time(), **extra}
    line = json.dumps(record)
    try:
        os.makedirs(os.path.dirname(ALERT_LOG), exist_ok=True)
        with open(ALERT_LOG, "a") as f:
            f.write(line + "\n")
    except OSError as e:
        print(f"WARNING: could not write alert log: {e}")
    print(f"[fog-node] {line}")


def publish_alert(client: mqtt.Client, patient_id: str, score_result: dict, fall: bool):
    payload = {
        "patient_id": patient_id,
        "alert": True,
        "reason": "fall_detected" if fall else "risk_score_threshold",
        "risk_score": score_result["total_score"],
        "breakdown": score_result["breakdown"],
        "vitals": _patients[patient_id]["vitals"],
        "timestamp": time.time(),
    }
    payload_json = json.dumps(payload)
    client.publish(f"fog/{patient_id}/alert", payload_json, qos=1)
    aws_publisher.publish(f"fog/{patient_id}/alert", payload_json, qos=1)
    log_event("ALERT", patient_id, {
        "risk_score": score_result["total_score"],
        "reason": payload["reason"],
    })


def publish_summary(client: mqtt.Client, patient_id: str, score_result: dict):
    patient = _patients.get(patient_id)
    if not patient:
        return
    payload = {
        "patient_id": patient_id,
        "alert": False,
        "risk_score": score_result["total_score"],
        "breakdown": score_result["breakdown"],
        "vitals": patient["vitals"],
        "timestamp": time.time(),
    }
    payload_json = json.dumps(payload)
    client.publish(f"fog/{patient_id}/summary", payload_json, qos=0)
    aws_publisher.publish(f"fog/{patient_id}/summary", payload_json, qos=0)


def on_message(client, userdata, msg):
    # topic shape: patients/{patient_id}/vitals/{vital_type}
    parts = msg.topic.split("/")
    if len(parts) != 4:
        return
    _, patient_id, _, vital_type = parts

    try:
        data = json.loads(msg.payload.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return

    with _state_lock:
        patient = _get_patient(patient_id)

        if vital_type == "motion":
            patient["fall_detected"] = bool(data.get("fall_detected", False))
        else:
            patient["vitals"][vital_type] = data.get("value")

        score_result = compute_score(patient["vitals"])
        risk = classify(score_result["total_score"], patient["fall_detected"], RISK_THRESHOLD)

        now = time.time()
        if risk == "alert" and (now - patient["last_alert_time"]) >= ALERT_COOLDOWN_SEC:
            patient["last_alert_time"] = now
            publish_alert(client, patient_id, score_result, patient["fall_detected"])
            # a fall is a one-off event — clear it so it doesn't keep
            # re-triggering "alert" classification on later readings
            patient["fall_detected"] = False


def periodic_summary_loop(client: mqtt.Client):
    """Every SUMMARY_INTERVAL_SEC, send a summary for every known patient.
    This is the low-cost "everything's fine" heartbeat sent to the cloud
    when the fog node hasn't found a reason to alert."""
    while not _shutdown.is_set():
        _shutdown.wait(SUMMARY_INTERVAL_SEC)
        with _state_lock:
            for patient_id, patient in _patients.items():
                score_result = compute_score(patient["vitals"])
                publish_summary(client, patient_id, score_result)


def on_connect(client, userdata, flags, rc):
    print(f"[fog-node] Connected to MQTT {MQTT_HOST}:{MQTT_PORT} (rc={rc})")
    client.subscribe("patients/+/vitals/+", qos=1)


def main():
    print(f"[fog-node] Starting. RISK_THRESHOLD={RISK_THRESHOLD}, "
          f"SUMMARY_INTERVAL_SEC={SUMMARY_INTERVAL_SEC}, ALERT_COOLDOWN_SEC={ALERT_COOLDOWN_SEC}")

    client = mqtt.Client(client_id="fog-node", protocol=mqtt.MQTTv311)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)

    threading.Thread(target=periodic_summary_loop, args=(client,), daemon=True).start()

    client.loop_forever()


if __name__ == "__main__":
    main()
