# Smart Patient Health Alert System — Sensor Simulator Layer

Simulates 5 vital-sign sensors per patient (heart rate, SpO2, respiration
rate, body temperature, motion/fall) as required by the H9FECC brief. No
physical hardware — everything is generated in software and published over
MQTT, which stands in locally for AWS IoT Core until the backend 
is wired up.

## Layout

patient-monitor/
    docker-compose.yml       # mosquitto broker + 3 patient simulators
    mosquitto/config/        # local MQTT broker config (anonymous, dev-only)
    sensors/
       simulator.py         # one process = one patient, 1 thread per vital
       vitals.py            # value generation + deterioration drift model
       requirements.txt
       Dockerfile
    shared-data/             # ground_truth.log gets written here (bind mount)


## Run it

```bash
docker compose up --build
```

This starts:
- `mosquitto` — MQTT broker on `localhost:1883`
- `patient-1` — stable the whole run (control)
- `patient-2` — **scripted deterioration**: stable for 30s, then drifts to
  critical values over 60s. This is the ground-truth run for the
  fog-vs-cloud latency experiment in days 6-7.
- `patient-3` — stable (spare, useful once the dashboard exists, and for
  the optional multi-patient stretch goal)

## Verify readings are flowing

In another terminal (needs `mosquitto-clients` installed, or run it inside
the mosquitto container):

```bash
docker exec -it mosquitto mosquitto_sub -t 'patients/#' -v
```

You should see JSON messages like:

```
patients/patient-2/vitals/heart_rate {"patient_id": "patient-2", "vital_type": "heart_rate", "value": 74.2, "unit": "bpm", "seq": 12, "timestamp": 1751878213.4}
```

Watch `patient-2`'s heart_rate/spo2/respiration_rate/body_temp values start
drifting toward dangerous values ~30s after startup.

## Ground truth log

`shared-data/ground_truth.log` records the exact wall-clock timestamp each
scripted event happens (deterioration start, any fall). This is what you'll
diff the fog node's/cloud's alert timestamp against to compute detection
latency for the headline experiment.

## Configuration knobs (env vars, set in docker-compose.yml)

| Variable | Purpose | Default |
|---|---|---|
| `SCENARIO` | `stable` or `deteriorating` | `stable` |
| `SCENARIO_START_SEC` | delay before deterioration begins | `30` |
| `DETERIORATION_DURATION_SEC` | how long the drift to critical takes | `60` |
| `HR_FREQ_SEC`, `SPO2_FREQ_SEC`, `RESP_FREQ_SEC`, `TEMP_FREQ_SEC`, `MOTION_FREQ_SEC` | per-sensor publish frequency | 2/3/3/5/1s |

To add more simulated patients (e.g. for the multi-patient CloudWatch
concurrency stretch goal), duplicate a `patient-N` block in
`docker-compose.yml` with a new `PATIENT_ID` and `SCENARIO`.

## Day 2: The fog node

`fog-node/` is the actual "fog computing" piece of the project — it
subscribes to every patient's raw vitals (`patients/+/vitals/+`), keeps
the latest reading of each vital per patient in memory, and on every
update recomputes a composite early-warning score (`scoring.py`, modeled
loosely on hospital NEWS/MEWS scores). If that score crosses
`RISK_THRESHOLD`, or a fall is detected, it publishes an **immediate
alert** to `fog/{patient_id}/alert`. Otherwise it just publishes a
**periodic summary** every `SUMMARY_INTERVAL_SEC` to
`fog/{patient_id}/summary`. This local scoring — deciding at the edge
instead of shipping every raw reading to the cloud — is the whole "why
fog computing matters" argument for this project.

Watch fog node output directly:

```bash
docker exec -it mosquitto mosquitto_sub -t 'fog/#' -v
```

You should see periodic `fog/patient-1/summary` and `fog/patient-3/summary`
messages every 15s (they stay stable), and an `fog/patient-2/alert` message
fire once patient-2's score crosses the threshold during its scripted
deterioration — plus another alert around the 45s mark when its scripted
fall triggers.

Every alert is also appended to `shared-data/fog_alerts.log` with a
timestamp — diff this against `shared-data/ground_truth.log`'s
`deterioration_started` / `fall_triggered` timestamps to get the fog
node's detection latency, which is the headline number for the days 6-7
experiment. `RISK_THRESHOLD` (set in `docker-compose.yml`) is the
sensitivity knob to vary across that experiment (try 3, 5, 7).

## Running different experiment scenarios

`docker-compose.yml` on its own is the **clean deterioration-only**
scenario (patient-2 gradually deteriorates, no fall). Two override files
let you run other scenarios without editing the base file:

```bash
# Deterioration-only (default) — for measuring risk-score alert latency
docker compose up --build

# Fall-only — patient-2 stays stable except for one scripted fall at 20s.
# Isolates fall-detection latency cleanly.
docker compose -f docker-compose.yml -f docker-compose.fall-only.yml up --build

# Combined — deterioration + a fall partway through. Good for a live demo
# (shows both alert types), but NOT for latency measurement — the fall
# alert's cooldown can delay/mask the risk-score alert.
docker compose -f docker-compose.yml -f docker-compose.combined.yml up --build
```

**Important: before each new experiment run**, clear the old logs so you're
not reading stale data from a previous run:

```bash
docker compose down
del shared-data\ground_truth.log      # PowerShell; use rm on Mac/Linux
del shared-data\fog_alerts.log
```

Also skip `--build` after the first time you've built each scenario —
rebuilding delays container startup and skews your very first latency
measurement (this happened once already; harmless, just noisy).

### Reading the results

```bash
type shared-data\ground_truth.log     # when did the scripted event actually happen
type shared-data\fog_alerts.log       # when did the fog node alert
```

```
detection_latency = first_alert_timestamp - ground_truth_event_timestamp
```

Only use the **first** alert line after each ground-truth event — the fog
node deliberately keeps re-alerting every `ALERT_COOLDOWN_SEC` while risk
stays high (sensible for a real system, but only the first one is your
detection-latency data point).

## Days 3-4: AWS backend chain setup (AWS Console, step by step)

Goal: `fog-node` publishes alerts/summaries to AWS IoT Core → an IoT Rule
routes them to SQS → Lambda (triggered by SQS) writes them to DynamoDB.
The local Mosquitto pipeline keeps working unchanged throughout — this is
purely additive.

### Step 1 — DynamoDB table (do this first, other steps reference it)
1. AWS Console → **DynamoDB** → Create table
2. Table name: `PatientEvents`
3. Partition key: `patient_id` (String)
4. Sort key: `timestamp` (Number)
5. Leave defaults for everything else → Create table

### Step 2 — SQS queue
1. AWS Console → **SQS** → Create queue
2. Type: **Standard**
3. Name: `patient-alerts-queue`
4. Leave defaults → Create queue
5. Note its **ARN** (shown on the queue's detail page) — you'll need it
   in Step 4.

### Step 3 — Lambda function
1. AWS Console → **Lambda** → Create function
2. Author from scratch, name: `processPatientEvent`
3. Runtime: **Python 3.12**
4. Execution role: **Use an existing role** → select the Learner Lab's
   `LabRole` (Learner Lab doesn't let you create custom IAM roles — this
   is expected, not a mistake)
5. Create function
6. In the **Code** tab, replace the default code with the contents of
   `lambda/process_patient_event.py` from this project → Deploy
7. **Configuration** tab → **Environment variables** → Add:
   `DYNAMODB_TABLE` = `PatientEvents`
8. **Configuration** tab → **Triggers** → Add trigger → **SQS** → select
   `patient-alerts-queue` → Add
   (This handles all the polling — no extra code needed for that part.)

### Step 4 — AWS IoT Core: create a "thing" + certificate
1. AWS Console → **IoT Core** → Manage → All devices → **Things** →
   Create things → **Create single thing**
2. Name: `fog-node-patient-monitor` → Next
3. Device certificate: **Auto-generate a new certificate** → Next
4. Attach a policy — if none exists yet, open a new tab and create one
   first (**Security → Policies → Create policy**):
   - Name: `fog-node-publish-policy`
   - Policy document (JSON tab), adjust the region/account placeholders
     shown in the console's own example, action `iot:Connect` and
     `iot:Publish` on resource `arn:aws:iot:*:*:topic/fog/*` and
     `arn:aws:iot:*:*:client/fog-node-aws`
   - Create, then go back and attach it to the thing
5. **Download all 3 files** when prompted:
   - `certificate.pem.crt`
   - `private.pem.key`
   - Amazon Root CA 1 (`AmazonRootCA1.pem`) — separate download link on
     the same page
6. Put all 3 files into this project's `certs/` folder, named exactly:
   ```
   certs/certificate.pem.crt
   certs/private.pem.key
   certs/AmazonRootCA1.pem
   ```
7. Note your **IoT endpoint**: IoT Core → **Settings** (left sidebar) →
   copy the "Device data endpoint" (looks like
   `a1b2c3d4e5-ats.iot.<region>.amazonaws.com`)

### Step 5 — IoT Rule: route messages to SQS
1. IoT Core → **Message routing** → **Rules** → Create rule
2. Name: `routeAlertsToSQS`
3. SQL statement: `SELECT * FROM 'fog/+/+'` (catches both
   `fog/{id}/alert` and `fog/{id}/summary` with one rule)
4. Rule action: **Send a message to an SQS queue**
   - Queue: `patient-alerts-queue`
   - Use base64 encoding: **No**
5. IAM role: use `LabRole` again
6. Create

### Step 6 — Point the fog node at AWS
In `docker-compose.yml`, set:
```yaml
AWS_IOT_ENDPOINT: "a1b2c3d4e5-ats.iot.<region>.amazonaws.com"   # your endpoint from Step 4.7
```
Then:
```bash
docker compose down
docker compose up --build
```
Look for this line in the fog-node container's logs:
```
[aws-publisher] Connected to AWS IoT Core at ...
```

### Step 7 — Verify data reaches DynamoDB
1. Let patient-2 go through its deterioration scenario (~90s)
2. AWS Console → **DynamoDB** → Tables → `PatientEvents` → **Explore
   table items**
3. You should see items appearing with `patient_id`, `timestamp`,
   `risk_score`, etc. — matching what's in your local
   `shared-data/fog_alerts.log`

If nothing shows up, check in this order: (a) fog-node logs for
`[aws-publisher]` connection errors, (b) IoT Core → **Monitor** →
MQTT test client → subscribe to `fog/#` to confirm messages are arriving
at IoT Core at all, (c) the IoT Rule's own error metrics (IoT Core →
Message routing → Rules → click the rule → Monitor tab) for delivery
failures to SQS, (d) Lambda → Monitor tab → CloudWatch Logs for errors in
`processPatientEvent`.




Wire the AWS backend chain: swap the fog node's local MQTT alert/summary
publish calls for AWS IoT Core, feeding into SQS -> Lambda -> DynamoDB.
