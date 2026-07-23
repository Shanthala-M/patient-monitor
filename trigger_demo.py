"""
trigger_demo.py

For the LIVE DEMO chosen patient start deteriorating immediately — no waiting on the timer. 
This is the tool that solves "I can't predict the exact moment to show the class".

Usage (from the project root, with your pipeline already running via
`docker compose up`):

    python trigger_demo.py patient-2

That's it. Within a couple of seconds you should see:
  - The dashboard's patient-2 card start climbing/flip to red "Alert"
  - (if using the MQTT test client / mosquitto_sub) messages on
    fog/patient-2/alert

Requires: paho-mqtt 
If you don't have it installed outside Docker, run:
    pip install paho-mqtt==1.6.1
or on Windows if that's your default Python:
    py -3 -m pip install paho-mqtt==1.6.1

Then trigger with:
    py -3 trigger_demo.py patient-2
"""

import sys
import time

import paho.mqtt.client as mqtt

MQTT_HOST = "localhost"  # your machine, since port 1883 is published to the host
MQTT_PORT = 1883


def main():
    if len(sys.argv) != 2:
        print("Usage: python trigger_demo.py <patient_id>")
        print("Example: python trigger_demo.py patient-2")
        sys.exit(1)

    patient_id = sys.argv[1]
    topic = f"patients/{patient_id}/control/trigger"

    client = mqtt.Client(client_id="demo-trigger")
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=10)
    client.loop_start()

    time.sleep(0.5)  # let the connection settle before publishing
    client.publish(topic, payload="go", qos=1)
    print(f"Triggered deterioration for {patient_id} (published to {topic}).")
    print("Check your dashboard / mosquitto_sub / MQTT test client now.")

    time.sleep(1)  # give the publish time to actually send before exiting
    client.loop_stop()
    client.disconnect()


if __name__ == "__main__":
    main()
