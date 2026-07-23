# Lambda function triggered by the SQS queue. Each message is an
# alert/summary from the fog node, forwarded through IoT Core -> the IoT
# rule -> SQS. Just writes each one to DynamoDB.
# Table: PatientEvents, partition key patient_id (String), sort key timestamp (Number).
# Deploy by pasting this straight into the Lambda console code editor -
# no extra dependencies, boto3 is already in the runtime.

import json
import os
import time
import boto3
from decimal import Decimal

TABLE_NAME = os.environ.get("DYNAMODB_TABLE", "PatientEvents")

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)


def _to_decimal(obj):
    # boto3's DynamoDB resource wants Decimal, not float
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

            if "patient_id" not in item or "timestamp" not in item:
                print(f"WARNING: skipping malformed event, missing keys: {body}")
                failed += 1
                continue

            # separate from the fog node's own "timestamp" - this one
            # marks when it actually landed here, so we can measure the
            # cloud-hop time on top of the fog node's decision time
            item["dynamo_received_at"] = Decimal(str(time.time()))

            table.put_item(Item=item)
            processed += 1

        except Exception as e:
            print(f"ERROR processing record: {e}")
            failed += 1
            # not re-raising here - that would make SQS retry/redeliver
            # the message, and logging + moving on is simpler than
            # dealing with poison-pill messages blocking the queue

    print(f"Processed {processed} events, {failed} failed, "
          f"out of {len(event.get('Records', []))} total.")

    return {"statusCode": 200, "processed": processed, "failed": failed}
