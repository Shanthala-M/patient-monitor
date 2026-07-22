"""
app.py

Flask dashboard for the Smart Patient Health Alert System.

Runs as a plain local Python process (NOT inside Docker) — deliberately
kept separate from the sensor/fog-node containers, since it just needs to
read from DynamoDB like any other client of the cloud backend. This also
sidesteps having to pass AWS Learner Lab's temporary session credentials
into a container.

Two routes:
    GET /            -> renders the dashboard page (initial data)
    GET /api/patients -> JSON: latest status for every known patient
                          (polled by the page's own JS every few seconds
                          for a "live" feel, no full page reload)

Configuration (env vars, all optional):
    AWS_REGION       default "us-east-1"
    DYNAMODB_TABLE   default "PatientEvents"
    PATIENT_IDS      comma-separated list, default "patient-1,patient-2,patient-3,patient-4"
"""

import os
import time

import boto3
from flask import Flask, jsonify, render_template
from boto3.dynamodb.conditions import Key, Attr

# Load AWS_* variables from a local .env file if present (see README) —
# this file is gitignored and never leaves your machine. Falls back
# silently to normal environment variables if python-dotenv isn't
# installed or .env doesn't exist.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
TABLE_NAME = os.environ.get("DYNAMODB_TABLE", "PatientEvents")
PATIENT_IDS = os.environ.get("PATIENT_IDS", "patient-1,patient-2,patient-3,patient-4").split(",")

# Must match fog-node's RISK_THRESHOLD (docker-compose.yml). Used to derive
# alert status directly from the risk score, rather than trusting the
# stored "alert" flag literally — the fog node's periodic heartbeat
# summaries always hardcode alert=False even when the vitals they report
# happen to be elevated at that instant (they're just a liveness signal,
# not a fresh classification). Recomputing from the score here keeps the
# dashboard self-consistent regardless of which message type it's reading.
RISK_THRESHOLD = int(os.environ.get("RISK_THRESHOLD", "3"))

# Normal healthy ranges, matching sensors/vitals.py's VITAL_CONFIG — shown
# on the dashboard so each number is self-explaining without narration.
NORMAL_RANGES = {
    "heart_rate": {"low": 60, "high": 90, "unit": "bpm", "label": "Heart rate"},
    "spo2": {"low": 96, "high": 99, "unit": "%", "label": "SpO2"},
    "respiration_rate": {"low": 12, "high": 18, "unit": "br/min", "label": "Resp. rate"},
    "body_temp": {"low": 36.5, "high": 37.3, "unit": "\u00b0C", "label": "Body temp"},
}

app = Flask(__name__)

dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
table = dynamodb.Table(TABLE_NAME)


def get_latest_event(patient_id: str):
    """Query DynamoDB for this patient's single most recent event
    (alert or summary — whichever happened last), using the table's
    existing partition key (patient_id) + sort key (timestamp)."""
    response = table.query(
        KeyConditionExpression=Key("patient_id").eq(patient_id),
        ScanIndexForward=False,  # descending by sort key -> newest first
        Limit=1,
    )
    items = response.get("Items", [])
    return items[0] if items else None


def _clean(value):
    """DynamoDB returns Decimal for numbers; convert to plain
    int/float so Flask's JSON encoder and Jinja can handle it cleanly."""
    if hasattr(value, "__float__"):
        f = float(value)
        return int(f) if f.is_integer() else round(f, 1)
    return value


def build_vitals_detail(raw_vitals: dict) -> list:
    """Turns {"heart_rate": 148, ...} into an ordered list of dicts with
    range/unit/out-of-range info attached, so the template doesn't need
    to know anything about medical ranges — it just renders what it's given."""
    detail = []
    for vital_type, config in NORMAL_RANGES.items():
        value = _clean(raw_vitals.get(vital_type)) if vital_type in raw_vitals else None
        out_of_range = (
            value is not None and (value < config["low"] or value > config["high"])
        )
        detail.append({
            "key": vital_type,
            "label": config["label"],
            "value": value,
            "unit": config["unit"],
            "range_low": config["low"],
            "range_high": config["high"],
            "out_of_range": out_of_range,
        })
    return detail


def build_patient_view(patient_id: str) -> dict:
    event = get_latest_event(patient_id)

    if event is None:
        return {
            "patient_id": patient_id,
            "status": "no_data",
            "alert": False,
            "risk_score": None,
            "vitals": build_vitals_detail({}),
            "seconds_ago": None,
        }

    raw_vitals = {k: _clean(v) for k, v in event.get("vitals", {}).items()}
    timestamp = float(event.get("timestamp", 0))
    risk_score = _clean(event.get("risk_score", 0))

    # See RISK_THRESHOLD comment above: a stored item can be a periodic
    # heartbeat (alert=False) even while its risk_score is at or above
    # the threshold. Treat it as an alert either way so the badge always
    # matches the numbers the person is looking at.
    is_alert = bool(event.get("alert", False)) or (
        risk_score is not None and risk_score >= RISK_THRESHOLD
    )

    return {
        "patient_id": patient_id,
        "status": "alert" if is_alert else "stable",
        "alert": is_alert,
        "risk_score": risk_score,
        "vitals": build_vitals_detail(raw_vitals),
        "reason": event.get("reason"),
        "seconds_ago": max(0, int(time.time() - timestamp)),
    }


def get_recent_activity(limit: int = 12) -> list:
    """Builds the sidebar's live activity feed by querying each KNOWN
    patient for their most recent few events, then merging and sorting.

    Deliberately NOT a table-wide Scan: DynamoDB scans read in physical
    storage order, not time order, so a patient with many more stored
    items (e.g. one that's been running longest) can dominate a scan's
    results entirely, silently hiding every other patient's activity.
    Querying per patient_id (the table's actual partition key) avoids
    that skew and is cheap since the patient list is small and known."""
    all_items = []
    for patient_id in PATIENT_IDS:
        response = table.query(
            KeyConditionExpression=Key("patient_id").eq(patient_id),
            ScanIndexForward=False,  # newest first
            Limit=5,
        )
        all_items.extend(response.get("Items", []))

    all_items.sort(key=lambda i: float(i.get("timestamp", 0)), reverse=True)

    activity = []
    for item in all_items[:limit]:
        activity.append({
            "patient_id": item.get("patient_id"),
            "alert": bool(item.get("alert", False)),
            "risk_score": _clean(item.get("risk_score", 0)),
            "reason": item.get("reason"),
            "timestamp": float(item.get("timestamp", 0)),
            "seconds_ago": max(0, int(time.time() - float(item.get("timestamp", 0)))),
        })
    return activity


@app.route("/")
def index():
    patients = [build_patient_view(pid) for pid in PATIENT_IDS]
    activity = get_recent_activity()
    return render_template("index.html", patients=patients, activity=activity)


@app.route("/api/patients")
def api_patients():
    patients = [build_patient_view(pid) for pid in PATIENT_IDS]
    return jsonify(patients)


@app.route("/api/activity")
def api_activity():
    return jsonify(get_recent_activity())


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
