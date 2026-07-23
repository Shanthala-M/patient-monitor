# The fog node. Subscribes to every patient's raw vitals over MQTT,
# keeps the latest reading per patient in memory, and decides locally
# whether to alert - instead of sending everything to the cloud and
# waiting for a decision back.
#
# Stable patients just get a periodic "still alive" summary sent up.
# Anything crossing the risk threshold (or a fall) gets an alert
# published immediately. Alerts go both to the local broker and to AWS
# IoT Core via aws_publisher.py, so this works fine with or without AWS
# credentials configured.

import json
import os
import threading
import time

import paho.mqtt.client as mqtt

from scoring import compute_score, classify
import aws_publisher

MQTT_HOST = os.environ.get("MQTT_HOST", "mosquitto")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))

# lower threshold = more sensitive, alerts sooner but more false positives
RISK_THRESHOLD = int(os.environ.get("RISK_THRESHOLD", "5"))

SUMMARY_INTERVAL_SEC = float(os.environ.get("SUMMARY_INTERVAL_SEC", "15"))
ALERT_COOLDOWN_SEC = float(os.environ.get("ALERT_COOLDOWN_SEC", "20"))

ALERT_LOG = os.environ.get("ALERT_LOG", "/data/fog_alerts.log")

_state_lock = threading.Lock()
_patients = {}
# _patients[patient_id] = {"vitals": {...}, "fall_detected": bool, "last_alert_time": float}

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
    # topic looks like patients/{patient_id}/vitals/{vital_type}
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
            # clear the fall flag so it doesn't keep re-triggering alerts
            # on every reading after the fact
            patient["fall_detected"] = False


def periodic_summary_loop(client: mqtt.Client):
    # sends a "still fine" summary for every known patient every
    # SUMMARY_INTERVAL_SEC, regardless of risk level
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
