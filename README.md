# Smart Patient Health Alert System

A fog-computing pipeline for remote patient vital monitoring. Simulated
patients publish five vital-sign streams (heart rate, SpO2, respiration
rate, body temperature, motion/fall) which a local fog node scores in
real time and forwards to an AWS backend (IoT Core, SQS, Lambda,
DynamoDB) with a live dashboard.

## Architecture

```
patient-monitor/
├── docker-compose.yml            # base scenario: mosquitto + fog node + patients
├── docker-compose.fall-only.yml  # override: isolated fall-detection scenario
├── docker-compose.combined.yml   # override: deterioration + fall together
├── mosquitto/config/             # local MQTT broker config
├── sensors/
│   ├── simulator.py              # one process per patient, one thread per vital
│   ├── vitals.py                 # value generation + deterioration drift model
│   ├── requirements.txt
│   └── Dockerfile
├── fog-node/
│   ├── fog_node.py                # subscribes to all patients, scores, decides alert/summary
│   ├── scoring.py                 # composite risk score (adapted from NEWS2)
│   ├── aws_publisher.py           # secondary MQTT connection to AWS IoT Core over TLS
│   ├── requirements.txt
│   └── Dockerfile
├── lambda/
│   └── process_patient_event.py   # SQS-triggered, writes events to DynamoDB
├── lambda-dashboard/
│   └── dashboard_handler.py       # serves the live dashboard via a public Function URL
├── dashboard/                     # local Flask version of the dashboard (dev/reference)
├── certs/                         # AWS IoT certificates (gitignored)
├── shared-data/                   # ground_truth.log, fog_alerts.log
├── trigger_demo.py                # manually trigger a patient's deterioration on demand
├── measure_cloud_latency.py       # computes detection latency from local + cloud timestamps
└── README.md
```

Sensors publish raw readings over MQTT. The fog node subscribes to every
patient, maintains their latest vitals, and recomputes a composite risk
score on every update. If the score crosses a threshold, or a fall is
detected, it publishes an immediate alert; otherwise it publishes a
periodic summary. Only these decisions — not raw readings — are
forwarded to AWS, where an IoT Rule routes them through SQS into a
Lambda function that writes to DynamoDB. A second Lambda function serves
a live dashboard directly from DynamoDB via a public Function URL.

## Running the sensor and fog layer locally

```bash
docker compose up --build
```

This starts:
- `mosquitto` — local MQTT broker on `localhost:1883`
- `patient-1` — stable for the whole run (control)
- `patient-2` — scripted deterioration: stable for 30s, then drifts
  toward critical values over 60s
- `patient-3` — stable (spare)
- `patient-4` — auto-cycles stable/alert on a loop, for demos
- `fog-node` — subscribes to all patients, scores, publishes decisions

### Verifying readings are flowing

```bash
docker exec -it mosquitto mosquitto_sub -t 'patients/#' -v
```

You should see JSON messages such as:
```
patients/patient-2/vitals/heart_rate {"patient_id": "patient-2", "vital_type": "heart_rate", "value": 74.2, "unit": "bpm", "seq": 12, "timestamp": 1751878213.4}
```

Watch `patient-2`'s values start drifting toward dangerous ranges
roughly 30 seconds after startup.

### Watching the fog node's decisions

```bash
docker exec -it mosquitto mosquitto_sub -t 'fog/#' -v
```

Stable patients publish a periodic `fog/{id}/summary` message every
`SUMMARY_INTERVAL_SEC`; a patient crossing the risk threshold (or
triggering a fall) publishes an immediate `fog/{id}/alert`. Every alert
is also appended to `shared-data/fog_alerts.log` with a timestamp.

### Ground truth log

`shared-data/ground_truth.log` records the exact wall-clock timestamp of
each scripted event (deterioration start, fall trigger). This is used to
compute detection latency by diffing against the fog node's alert
timestamp.

## Configuration

Environment variables, set in `docker-compose.yml`:

| Variable | Purpose | Default |
|---|---|---|
| `SCENARIO` | `stable`, `deteriorating`, or `cycling` | `stable` |
| `SCENARIO_START_SEC` | delay before deterioration begins | `30` |
| `DETERIORATION_DURATION_SEC` | how long the drift to critical takes | `60` |
| `FALL_AT_SEC` | seconds until a single scripted fall (unset = none) | unset |
| `CYCLE_STABLE_SEC` / `CYCLE_ALERT_SEC` | for `cycling` scenario, duration of each phase | `20` / `20` |
| `HR_FREQ_SEC`, `SPO2_FREQ_SEC`, `RESP_FREQ_SEC`, `TEMP_FREQ_SEC`, `MOTION_FREQ_SEC` | per-sensor publish frequency | 2/3/3/5/1s |
| `RISK_THRESHOLD` | fog node's alert sensitivity | `3` |
| `SUMMARY_INTERVAL_SEC` | periodic summary frequency | `5` |
| `ALERT_COOLDOWN_SEC` | minimum gap between repeat alerts | `8` |
| `AWS_IOT_ENDPOINT` | AWS IoT Core device data endpoint (blank = local-only) | blank |

To add more simulated patients, duplicate a `patient-N` block in
`docker-compose.yml` with a new `PATIENT_ID` and `SCENARIO`.

## Running different scenarios

The base `docker-compose.yml` runs a clean deterioration-only scenario
for patient-2. Two override files add other scenarios without editing
the base file:

```bash
# Deterioration-only (default) — for measuring risk-score alert latency
docker compose up --build

# Fall-only — patient-2 stays stable except for one scripted fall at 20s
docker compose -f docker-compose.yml -f docker-compose.fall-only.yml up --build

# Combined — deterioration plus a fall partway through (useful for demos,
# not for latency measurement — the fall alert's cooldown can delay or
# mask the risk-score alert)
docker compose -f docker-compose.yml -f docker-compose.combined.yml up --build
```

Before each new measurement run, clear the previous run's logs:

```bash
docker compose down
del shared-data\ground_truth.log      # PowerShell; use rm on Mac/Linux
del shared-data\fog_alerts.log
```

Skip `--build` on repeat runs once images already exist — rebuilding
delays container startup and can skew the first latency measurement of a
run.

### Reading latency results locally

```bash
type shared-data\ground_truth.log     # when the scripted event actually happened
type shared-data\fog_alerts.log       # when the fog node alerted
```

```
detection_latency = first_alert_timestamp - ground_truth_event_timestamp
```

Use only the first alert line after each ground-truth event — the fog
node continues re-alerting every `ALERT_COOLDOWN_SEC` while risk stays
high, which is correct behaviour but not part of the latency figure.

### Measuring cloud-inclusive latency

`measure_cloud_latency.py` joins the local ground truth log with
DynamoDB to report both the fog node's own decision latency and the
additional time for the event to travel through IoT Core, SQS, and
Lambda into DynamoDB:

```bash
py -3 measure_cloud_latency.py patient-2
```

## AWS backend setup

Goal: the fog node publishes alerts/summaries to AWS IoT Core, an IoT
Rule routes them to SQS, and a Lambda function (triggered by SQS) writes
them to DynamoDB. The local MQTT pipeline keeps working unchanged
throughout.

### 1. DynamoDB table
1. AWS Console → **DynamoDB** → Create table
2. Table name: `PatientEvents`
3. Partition key: `patient_id` (String)
4. Sort key: `timestamp` (Number)
5. Leave defaults → Create table

### 2. SQS queue
1. AWS Console → **SQS** → Create queue
2. Type: Standard
3. Name: `patient-alerts-queue`
4. Leave defaults → Create queue

### 3. Ingestion Lambda
1. AWS Console → **Lambda** → Create function
2. Author from scratch, name: `processPatientEvent`
3. Runtime: Python 3.12
4. Execution role: use an existing role → `LabRole`
5. Paste the contents of `lambda/process_patient_event.py` into the
   code editor → Deploy
6. Configuration → Environment variables → add `DYNAMODB_TABLE` =
   `PatientEvents`
7. Configuration → Triggers → Add trigger → SQS → select
   `patient-alerts-queue`

### 4. IoT Core: thing and certificate
1. AWS Console → **IoT Core** → Manage → All devices → Things → Create
   things → Create single thing
2. Name: `fog-node-patient-monitor` → Next
3. Device certificate: auto-generate a new certificate → Next
4. Attach a policy (create one first if needed, under Security →
   Policies): allow `iot:Connect` on
   `arn:aws:iot:*:*:client/fog-node-aws` and `iot:Publish` on
   `arn:aws:iot:*:*:topic/fog/*`
5. Download all three files (certificate, private key, Amazon Root CA 1)
   and place them in `certs/`, named exactly:
   ```
   certs/certificate.pem.crt
   certs/private.pem.key
   certs/AmazonRootCA1.pem
   ```
6. Note the device data endpoint from IoT Core → Settings

### 5. IoT Rule
1. IoT Core → Message routing → Rules → Create rule
2. Name: `routeAlertsToSQS`
3. SQL statement: `SELECT * FROM 'fog/+/+'`
4. Action: send a message to an SQS queue → `patient-alerts-queue`,
   base64 encoding off
5. IAM role: `LabRole`

### 6. Point the fog node at AWS
In `docker-compose.yml`, set:
```yaml
AWS_IOT_ENDPOINT: "your-endpoint-here-ats.iot.<region>.amazonaws.com"
```
Then `docker compose down && docker compose up --build` and look for
`[aws-publisher] Connected to AWS IoT Core at ...` in the fog-node logs.

### 7. Verify data reaches DynamoDB
Let patient-2 complete its deterioration scenario, then check
DynamoDB → `PatientEvents` → Explore table items for new rows matching
`shared-data/fog_alerts.log`.

If nothing appears, check in order: fog-node logs for
`[aws-publisher]` connection errors; IoT Core's MQTT test client
subscribed to `fog/#`; the IoT Rule's monitor tab for delivery failures;
Lambda's CloudWatch logs for processing errors.

## Dashboard

Two implementations exist:

- **`lambda-dashboard/dashboard_handler.py`** — deployed as a second
  Lambda function behind a public Function URL. This is the live,
  cloud-hosted version.
- **`dashboard/app.py`** — a local Flask version with the same logic,
  useful for development without needing to redeploy a Lambda for every
  change.

### Deploying the Lambda dashboard
1. Create a new Lambda function, name `patientDashboard`, runtime
   Python 3.12, execution role `LabRole`
2. Paste in `lambda-dashboard/dashboard_handler.py` → Deploy
3. Environment variables: `DYNAMODB_TABLE=PatientEvents`,
   `PATIENT_IDS=patient-1,patient-2,patient-3,patient-4`,
   `RISK_THRESHOLD=3`
4. Configuration → Function URL → Create → Auth type NONE
5. The generated URL is the live dashboard

### Running the local Flask dashboard
```bash
cd dashboard
pip install -r requirements.txt
python app.py
```
Open `http://localhost:5000`. Needs AWS credentials set as environment
variables, or copy `dashboard/.env.example` to `dashboard/.env` and fill
in real values (gitignored, never commit real credentials).

## Demo

`patient-4` auto-cycles between stable and alert on its own (default: 20
seconds each), so it reliably shows an alert within any ~40-second
window without needing a manual trigger. Widen the alert window in
`docker-compose.yml` (`CYCLE_STABLE_SEC` / `CYCLE_ALERT_SEC`) for more
headroom during a longer demo.

For triggering a specific named patient at an exact moment instead, use:
```bash
py -3 trigger_demo.py patient-2
```
This publishes to that patient's MQTT control topic
(`patients/{id}/control/trigger`) and starts deterioration and a fall
immediately, bypassing any timer.
