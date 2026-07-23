# Second MQTT connection just for publishing up to AWS IoT Core. The fog
# node's main connection to the local broker is untouched - this is
# purely additive, and if AWS_IOT_ENDPOINT isn't set it just does
# nothing, so the local pipeline still works fine without AWS.
#
# Needs 3 cert files mounted at /certs, downloaded when you create the
# IoT thing in the console:
#   certificate.pem.crt, private.pem.key, AmazonRootCA1.pem

import os
import ssl
import threading

import paho.mqtt.client as mqtt

AWS_IOT_ENDPOINT = os.environ.get("AWS_IOT_ENDPOINT", "")
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
    global _client

    if not _enabled:
        return

    with _lock:
        if _client is None:
            _client = _build_client()
        if _client is None:
            return

    try:
        _client.publish(topic, payload, qos=qos)
    except Exception as e:
        print(f"[aws-publisher] WARNING: publish failed: {e}")
