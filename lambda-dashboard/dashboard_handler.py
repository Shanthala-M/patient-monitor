# The dashboard, as a plain Lambda function instead of Flask. Deploys
# the same way as processPatientEvent (paste into console, attach
# LabRole) and gets served publicly through a Lambda Function URL - no
# EC2, no Docker, nothing extra to package.
#
# To deploy: new Lambda function, Python 3.12, paste this in, LabRole,
# then Configuration -> Function URL -> Auth type NONE to get a public
# https://....lambda-url.<region>.on.aws/ address.
#
# Env vars: DYNAMODB_TABLE, PATIENT_IDS (comma-separated),
# RISK_THRESHOLD (needs to match what the fog node is using)
#
# Routes:
#   GET /             -> full HTML page
#   GET /api/patients -> JSON, latest status per patient
#   GET /api/activity -> JSON, recent activity across all patients

import json
import os
import time
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
TABLE_NAME = os.environ.get("DYNAMODB_TABLE", "PatientEvents")
PATIENT_IDS = os.environ.get("PATIENT_IDS", "patient-1,patient-2,patient-3,patient-4").split(",")
RISK_THRESHOLD = int(os.environ.get("RISK_THRESHOLD", "3"))

NORMAL_RANGES = {
    "heart_rate": {"low": 60, "high": 90, "unit": "bpm", "label": "Heart rate"},
    "spo2": {"low": 96, "high": 99, "unit": "%", "label": "SpO2"},
    "respiration_rate": {"low": 12, "high": 18, "unit": "br/min", "label": "Resp. rate"},
    "body_temp": {"low": 36.5, "high": 37.3, "unit": "\u00b0C", "label": "Body temp"},
}

dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
table = dynamodb.Table(TABLE_NAME)


# ---- data layer -------------------

def _clean(value):
    if isinstance(value, Decimal):
        f = float(value)
        return int(f) if f.is_integer() else round(f, 1)
    return value


def get_latest_event(patient_id):
    response = table.query(
        KeyConditionExpression=Key("patient_id").eq(patient_id),
        ScanIndexForward=False,
        Limit=1,
    )
    items = response.get("Items", [])
    return items[0] if items else None


def build_vitals_detail(raw_vitals):
    detail = []
    for vital_type, config in NORMAL_RANGES.items():
        value = _clean(raw_vitals.get(vital_type)) if vital_type in raw_vitals else None
        out_of_range = value is not None and (value < config["low"] or value > config["high"])
        detail.append({
            "key": vital_type, "label": config["label"], "value": value,
            "unit": config["unit"], "range_low": config["low"],
            "range_high": config["high"], "out_of_range": out_of_range,
        })
    return detail


def build_patient_view(patient_id):
    event = get_latest_event(patient_id)
    if event is None:
        return {
            "patient_id": patient_id, "status": "no_data", "alert": False,
            "risk_score": None, "vitals": build_vitals_detail({}), "seconds_ago": None,
        }

    raw_vitals = {k: _clean(v) for k, v in event.get("vitals", {}).items()}
    timestamp = float(event.get("timestamp", 0))
    risk_score = _clean(event.get("risk_score", 0))
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


def get_recent_activity(limit=12):
    all_items = []
    for patient_id in PATIENT_IDS:
        response = table.query(
            KeyConditionExpression=Key("patient_id").eq(patient_id),
            ScanIndexForward=False,
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


# ---- HTML rendering -----

def render_vital_row(v):
    value_str = f'{v["value"]} {v["unit"]}' if v["value"] is not None else "--"
    value_color = "#dc2626" if v["out_of_range"] else "#1a2233"
    marker_pct = 20.0
    if v["value"] is not None:
        pct = ((v["value"] - v["range_low"]) / (v["range_high"] - v["range_low"])) * 60 + 20
        marker_pct = max(2, min(98, pct))
    marker_color = "#dc2626" if v["out_of_range"] else "#16a34a"
    return f"""
    <div class="vital-row">
      <div class="vital-top">
        <span class="vital-label">{v["label"]}</span>
        <span class="vital-value" style="color:{value_color}">{value_str}</span>
      </div>
      <div class="range-track">
        <div class="range-normal-zone"></div>
        <div class="range-marker" style="left:{marker_pct}%; background:{marker_color}"></div>
      </div>
      <div class="vital-range">normal: {v["range_low"]}&ndash;{v["range_high"]} {v["unit"]}</div>
    </div>"""


def render_patient_card(p):
    alert_class = "alert-state" if p["alert"] else ""
    badge_class = p["status"]
    badge_text = {"alert": "Alert", "stable": "Stable", "no_data": "No data"}[p["status"]]
    vitals_html = "".join(render_vital_row(v) for v in p["vitals"])
    score = p["risk_score"] if p["risk_score"] is not None else "-"
    return f"""
    <div class="card {alert_class}" id="card-{p["patient_id"]}" data-patient="{p["patient_id"]}">
      <div class="card-top">
        <span class="name">{p["patient_id"]}</span>
        <span class="badge {badge_class}">{badge_text}</span>
      </div>
      {vitals_html}
      <div class="footer-row">
        <span>Risk score</span>
        <span class="score">{score}</span>
      </div>
    </div>"""


def render_activity_item(a):
    alert_class = "is-alert" if a["alert"] else ""
    kind = "Alert" if a["alert"] else "Summary"
    return f"""
    <div class="activity-item {alert_class}">
      <div class="top"><span>{a["patient_id"]}</span><span>{kind}</span></div>
      <div class="meta">score {a["risk_score"]} &middot; {a["seconds_ago"]}s ago</div>
    </div>"""


def render_patient_list(patients):
    rows = ""
    for p in patients:
        rows += f"""
        <a class="patient-row" href="#card-{p["patient_id"]}">
          <span class="id">{p["patient_id"]}</span>
          <span class="status-dot {p["status"]}"></span>
        </a>"""
    return rows


PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Patient Monitor</title>
<style>
  :root {{
    --bg-page: #f7f8fa; --bg-sidebar: #ffffff; --bg-card: #ffffff;
    --bg-card-alert: #fff5f5; --border: #e5e8ee; --border-alert: #f3b8b8;
    --text-primary: #1a2233; --text-secondary: #6b7385; --text-muted: #9aa2b1;
    --stable: #16a34a; --stable-bg: #eefbf1; --alert: #dc2626; --alert-bg: #fdeded;
    --font-sans: -apple-system, "Segoe UI", Arial, sans-serif;
    --font-mono: "SF Mono", Consolas, monospace;
  }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: var(--font-sans); background: var(--bg-page); color: var(--text-primary); margin: 0; display: flex; min-height: 100vh; }}
  .sidebar {{ width: 240px; flex-shrink: 0; background: var(--bg-sidebar); border-right: 1px solid var(--border); padding: 1.5rem 1.25rem; display: flex; flex-direction: column; gap: 1.75rem; }}
  .brand {{ display: flex; align-items: center; gap: 9px; }}
  .brand .dot {{ width: 8px; height: 8px; border-radius: 50%; background: var(--stable); animation: pulse-dot 1.8s ease-in-out infinite; }}
  .brand h1 {{ font-size: 15px; font-weight: 600; margin: 0; }}
  .brand p {{ font-size: 11px; color: var(--text-muted); margin: 2px 0 0; }}
  @keyframes pulse-dot {{ 0%, 100% {{ opacity: 1; }} 50% {{ opacity: 0.4; }} }}
  .section-label {{ font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; color: var(--text-muted); margin: 0 0 8px; font-weight: 600; }}
  .patient-list {{ display: flex; flex-direction: column; gap: 2px; }}
  .patient-row {{ display: flex; align-items: center; justify-content: space-between; padding: 7px 8px; border-radius: 6px; font-size: 13px; text-decoration: none; color: var(--text-secondary); }}
  .patient-row:hover {{ background: var(--bg-page); }}
  .patient-row .id {{ color: var(--text-primary); font-weight: 500; }}
  .status-dot {{ width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }}
  .status-dot.stable {{ background: var(--stable); }}
  .status-dot.alert {{ background: var(--alert); }}
  .status-dot.no_data {{ background: var(--text-muted); }}
  .activity-feed {{ display: flex; flex-direction: column; gap: 6px; overflow-y: auto; }}
  .activity-item {{ font-size: 12px; padding: 7px 9px; border-radius: 6px; background: var(--bg-page); border-left: 3px solid var(--border); }}
  .activity-item.is-alert {{ border-left-color: var(--alert); background: var(--alert-bg); }}
  .activity-item .top {{ display: flex; justify-content: space-between; color: var(--text-primary); font-weight: 500; }}
  .activity-item .meta {{ color: var(--text-muted); margin-top: 2px; }}
  .main {{ flex: 1; padding: 1.75rem 2rem; min-width: 0; }}
  .main-header {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 1.5rem; }}
  .main-header h2 {{ font-size: 19px; font-weight: 600; margin: 0; }}
  .main-header p {{ font-size: 13px; color: var(--text-muted); margin: 4px 0 0; }}
  #updated-at {{ font-size: 12px; color: var(--text-muted); }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 14px; }}
  .card {{ background: var(--bg-card); border: 1px solid var(--border); border-radius: 10px; padding: 1rem 1.15rem; scroll-margin-top: 20px; }}
  .card.alert-state {{ background: var(--bg-card-alert); border-color: var(--border-alert); }}
  .card-top {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }}
  .card-top .name {{ font-weight: 600; font-size: 14px; }}
  .badge {{ font-size: 11px; padding: 3px 9px; border-radius: 5px; font-weight: 600; }}
  .badge.stable {{ background: var(--stable-bg); color: var(--stable); }}
  .badge.alert {{ background: var(--alert-bg); color: var(--alert); }}
  .badge.no_data {{ background: var(--bg-page); color: var(--text-muted); }}
  .vital-row {{ margin-bottom: 9px; }}
  .vital-row:last-child {{ margin-bottom: 0; }}
  .vital-top {{ display: flex; justify-content: space-between; align-items: baseline; font-size: 12px; margin-bottom: 4px; }}
  .vital-label {{ color: var(--text-secondary); }}
  .vital-value {{ font-family: var(--font-mono); font-size: 13px; font-weight: 500; }}
  .vital-range {{ font-size: 10px; color: var(--text-muted); }}
  .range-track {{ position: relative; height: 4px; border-radius: 2px; background: var(--border); margin-bottom: 3px; }}
  .range-normal-zone {{ position: absolute; left: 20%; width: 60%; height: 100%; background: #bbf0cc; border-radius: 2px; }}
  .range-marker {{ position: absolute; top: -2.5px; width: 9px; height: 9px; border-radius: 50%; border: 2px solid var(--bg-card); transform: translateX(-50%); }}
  .footer-row {{ border-top: 1px solid var(--border); margin-top: 10px; padding-top: 8px; display: flex; justify-content: space-between; align-items: center; font-size: 12px; color: var(--text-muted); }}
  .footer-row .score {{ font-family: var(--font-mono); font-size: 14px; font-weight: 600; color: var(--text-primary); }}
  .card.alert-state .footer-row .score {{ color: var(--alert); }}
</style>
</head>
<body>
  <div class="sidebar">
    <div class="brand">
      <span class="dot"></span>
      <div><h1>Patient monitor</h1><p>Fog-layer early warning system</p></div>
    </div>
    <div>
      <p class="section-label">Patients</p>
      <div class="patient-list" id="patient-list">{patient_list}</div>
    </div>
    <div style="flex: 1; min-height: 0; display: flex; flex-direction: column;">
      <p class="section-label">Recent activity</p>
      <div class="activity-feed" id="activity-feed">{activity_items}</div>
    </div>
  </div>
  <div class="main">
    <div class="main-header">
      <div><h2>Live vitals</h2><p>Green band shows the healthy range for each reading</p></div>
      <span id="updated-at">loading...</span>
    </div>
    <div class="grid" id="patient-grid">{patient_cards}</div>
  </div>
<script>
async function refresh() {{
  try {{
    const res = await fetch("/api/patients");
    const patients = await res.json();
    for (const p of patients) {{
      const card = document.querySelector(`[data-patient="${{p.patient_id}}"]`);
      if (!card) continue;
      card.classList.toggle("alert-state", !!p.alert);
      const badge = card.querySelector(".badge");
      badge.className = "badge " + p.status;
      badge.textContent = p.status === "alert" ? "Alert" : (p.status === "stable" ? "Stable" : "No data");
    }}
    document.getElementById("updated-at").textContent = new Date().toLocaleTimeString();
  }} catch (e) {{ console.error(e); }}
}}
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>"""


def render_full_page():
    patients = [build_patient_view(pid) for pid in PATIENT_IDS]
    activity = get_recent_activity()
    return PAGE_TEMPLATE.format(
        patient_list=render_patient_list(patients),
        activity_items="".join(render_activity_item(a) for a in activity),
        patient_cards="".join(render_patient_card(p) for p in patients),
    )


# ---- Lambda entrypoint ---------------

def lambda_handler(event, context):
    path = event.get("rawPath", "/")

    if path == "/api/patients":
        patients = [build_patient_view(pid) for pid in PATIENT_IDS]
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(patients),
        }

    if path == "/api/activity":
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(get_recent_activity()),
        }

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "text/html"},
        "body": render_full_page(),
    }
