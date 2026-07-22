"""
measure_cloud_latency.py

Run this AFTER a clean deterioration run to get the full latency
breakdown for your report:

    ground_truth (deterioration started)
        -> fog_decision_latency ->
    fog node's alert timestamp (also in DynamoDB item's "timestamp")
        -> cloud_hop_latency ->
    dynamo_received_at (when Lambda actually wrote it to DynamoDB)

    total_cloud_inclusive_latency = dynamo_received_at - ground_truth

Usage:
    py -3 measure_cloud_latency.py patient-2

Requires the same AWS credentials as the dashboard (dashboard/.env, or
$env:AWS_* set in this terminal) plus boto3 installed
(py -3 -m pip install boto3 if needed).

Reads shared-data/ground_truth.log for the most recent
"deterioration_started" event for the given patient, then queries
DynamoDB for the first alert item at or after that time.
"""

import json
import os
import sys
import time
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

try:
    from dotenv import load_dotenv
    load_dotenv("dashboard/.env")
except ImportError:
    pass

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
TABLE_NAME = os.environ.get("DYNAMODB_TABLE", "PatientEvents")
GROUND_TRUTH_LOG = os.environ.get("GROUND_TRUTH_LOG", "shared-data/ground_truth.log")


def find_latest_ground_truth_event(patient_id: str, event_name: str = "deterioration_started"):
    """Read the local ground truth log and return the most recent
    matching event's timestamp for this patient."""
    if not os.path.isfile(GROUND_TRUTH_LOG):
        return None

    latest = None
    with open(GROUND_TRUTH_LOG) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("patient_id") != patient_id:
                continue
            if not record.get("event", "").startswith(event_name):
                continue
            if latest is None or record["timestamp"] > latest["timestamp"]:
                latest = record
    return latest


def find_first_alert_after(table, patient_id: str, after_timestamp: float):
    """Query DynamoDB for this patient's events at/after a given time,
    ascending, and return the first one flagged alert=True."""
    response = table.query(
        KeyConditionExpression=(
            Key("patient_id").eq(patient_id) & Key("timestamp").gte(Decimal(str(after_timestamp)))
        ),
        ScanIndexForward=True,  # ascending -> earliest first
    )
    for item in response.get("Items", []):
        if item.get("alert"):
            return item
    return None


def main():
    if len(sys.argv) != 2:
        print("Usage: py -3 measure_cloud_latency.py <patient_id>")
        sys.exit(1)

    patient_id = sys.argv[1]

    ground_truth = find_latest_ground_truth_event(patient_id)
    if ground_truth is None:
        print(f"No 'deterioration_started' event found for {patient_id} in "
              f"{GROUND_TRUTH_LOG}. Run a deterioration scenario first.")
        sys.exit(1)

    gt_time = ground_truth["timestamp"]
    print(f"Ground truth: {ground_truth['event']} at {gt_time}")

    dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
    table = dynamodb.Table(TABLE_NAME)

    print("Querying DynamoDB for the first alert at/after that time...")
    item = find_first_alert_after(table, patient_id, gt_time)

    if item is None:
        print("No alert found in DynamoDB yet. Either the pipeline hasn't "
              "finished processing, or AWS_IOT_ENDPOINT wasn't set for this run.")
        sys.exit(1)

    fog_alert_time = float(item["timestamp"])
    dynamo_time = float(item.get("dynamo_received_at", 0))

    fog_decision_latency = fog_alert_time - gt_time
    cloud_hop_latency = dynamo_time - fog_alert_time if dynamo_time else None
    total_latency = dynamo_time - gt_time if dynamo_time else None

    print()
    print("=" * 50)
    print(f"Patient:                     {patient_id}")
    print(f"Risk score at alert:         {item.get('risk_score')}")
    print(f"Reason:                      {item.get('reason')}")
    print("-" * 50)
    print(f"Fog decision latency:        {fog_decision_latency:.3f} s")
    if cloud_hop_latency is not None:
        print(f"Cloud-hop latency (IoT->SQS->Lambda->DynamoDB): {cloud_hop_latency:.3f} s")
        print(f"TOTAL cloud-inclusive latency: {total_latency:.3f} s")
    else:
        print("dynamo_received_at not present on this item — redeploy the "
              "updated Lambda code (adds this field) and re-run.")
    print("=" * 50)


if __name__ == "__main__":
    main()
