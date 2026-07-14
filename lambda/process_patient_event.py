"""
process_patient_event.py

AWS Lambda function. Trigger: SQS queue (configure this as an SQS trigger
on the Lambda in the console — no code needed for that wiring, AWS handles
polling the queue for you).

What it does:
  1. Each SQS message body is the JSON alert/summary published by the fog
     node (forwarded here via an IoT Core rule -> SQS).
  2. Writes one item per event into DynamoDB, table name from the
     DYNAMODB_TABLE environment variable (set this in the Lambda console
     under Configuration -> Environment variables).

DynamoDB table schema expected (create this first in the console):
    Partition key: patient_id   (String)
    Sort key:      timestamp    (Number)
  (This lets you query "all events for patient X, ordered by time" for
  both the dashboard and the latency-experiment analysis later.)

Deploy: paste this file's contents directly into the Lambda console's
inline code editor (Code tab), or zip it and upload — either works, no
external dependencies needed (boto3 is already available in the Lambda
Python runtime).
"""

import json
import os
import boto3
from decimal import Decimal

TABLE_NAME = os.environ.get("DYNAMODB_TABLE", "PatientEvents")

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)


def _to_decimal(obj):
    """DynamoDB's boto3 resource API doesn't accept plain Python floats —
    it requires Decimal. This recursively converts floats in the parsed
    JSON event to Decimal so table.put_item() doesn't raise."""
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _to_decimal(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_decimal(v) for v in obj]
    return obj


def lambda_handler(event, context):
    processed = 0
    failed = 0

    for record in event.get("Records", []):
        try:
            body = json.loads(record["body"])
            item = _to_decimal(body)

            # required keys for the table's key schema
            if "patient_id" not in item or "timestamp" not in item:
                print(f"WARNING: skipping malformed event, missing keys: {body}")
                failed += 1
                continue

            table.put_item(Item=item)
            processed += 1

        except Exception as e:
            print(f"ERROR processing record: {e}")
            failed += 1
            # Re-raising would cause SQS to retry/redeliver this message.
            # For a class project, logging and continuing is simpler and
            # avoids poison-pill messages blocking the queue. Mention this
            # trade-off in your report if asked about reliability.

    print(f"Processed {processed} events, {failed} failed, "
          f"out of {len(event.get('Records', []))} total.")

    return {"statusCode": 200, "processed": processed, "failed": failed}
