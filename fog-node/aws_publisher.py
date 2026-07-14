"""
aws_publisher.py

A second, independent MQTT connection used ONLY to publish alerts and
summaries up to AWS IoT Core. The fog node's main connection (in
fog_node.py) still talks to the local Mosquitto broker to receive raw
sensor readings — that doesn't change. This module just adds a cloud
publish path on top, so none of the scoring/decision logic needs to move
or change.

Enabled only if AWS_IOT_ENDPOINT is set (env var). If unset, publish()
is a no-op — so the whole local pipeline keeps working unmodified even
before AWS is wired up, and during local-only test runs.

Requires 3 files (downloaded when you create the IoT "thing" in the AWS
Console) mounted into the container at /certs:
    /certs/certificate.pem.crt   (the device certificate)
    /certs/private.pem.key       (the private key)
    /certs/AmazonRootCA1.pem     (Amazon's root CA, download link given
                                   in the console when creating the thing)
"""

import os
import ssl
import threading

import paho.mqtt.client as mqtt

AWS_IOT_ENDPOINT = os.environ.get("AWS_IOT_ENDPOINT", "")  # e.g. xxxx-ats.iot.eu-west-1.amazonaws.com
AWS_IOT_PORT = int(os.environ.get("AWS_IOT_PORT", "8883"))
CERT_DIR = os.environ.get("AWS_IOT_CERT_DIR", "/certs")

_client = None
_lock = threading.Lock()
_enabled = bool(AWS_IOT_ENDPOINT)


def _build_client():
    cert_path = os.path.join(CERT_DIR, "certificate.pem.crt")
    key_path = os.path.join(CERT_DIR, "private.pem.key")
    ca_path = os.path.join(CERT_DIR, "AmazonRootCA1.pem")

    for p in (cert_path, key_path, ca_path):
        if not os.path.isfile(p):
            print(f"[aws-publisher] WARNING: missing cert file {p} — "
                  f"AWS publishing disabled until certs are mounted correctly.")
            return None

    client = mqtt.Client(client_id="fog-node-aws", protocol=mqtt.MQTTv311)
    client.tls_set(
        ca_certs=ca_path,
        certfile=cert_path,
        keyfile=key_path,
        tls_version=ssl.PROTOCOL_TLSv1_2,
    )
    client.connect(AWS_IOT_ENDPOINT, AWS_IOT_PORT, keepalive=30)
    client.loop_start()
    print(f"[aws-publisher] Connected to AWS IoT Core at {AWS_IOT_ENDPOINT}:{AWS_IOT_PORT}")
    return client


def publish(topic: str, payload: str, qos: int = 1):
    """Publish to AWS IoT Core. Safe no-op if AWS_IOT_ENDPOINT isn't set
    or the connection hasn't been established yet (fails silently with a
    logged warning — local pipeline must never be blocked by this)."""
    global _client

    if not _enabled:
        return

    with _lock:
        if _client is None:
            _client = _build_client()
        if _client is None:
            return  # cert files missing, already warned above

    try:
        _client.publish(topic, payload, qos=qos)
    except Exception as e:
        print(f"[aws-publisher] WARNING: publish failed: {e}")
