#!/usr/bin/env python3
"""Publish representative Frigate + BirdNET-Go MQTT messages for local testing.

Usage:
    pip install paho-mqtt
    python scripts/publish_samples.py --host localhost --port 1883

Env overrides: MQTT_HOST, MQTT_PORT, MQTT_USER, MQTT_PASSWORD,
               FRIGATE_TOPIC (default frigate/events), BIRDNET_TOPIC (default birdnet)
"""

from __future__ import annotations

import argparse
import json
import os
import time
from typing import Optional

import paho.mqtt.client as mqtt

FRIGATE_TOPIC = os.environ.get("FRIGATE_TOPIC", "frigate/events")
BIRDNET_TOPIC = os.environ.get("BIRDNET_TOPIC", "birdnet")


def frigate_event(event_id: str, species: Optional[str], camera: str, score: float,
                  ts: float, msg_type: str = "end") -> dict:
    """A Frigate event message.

    ``species=None`` is the shape Frigate publishes when its own bird classification is
    turned off — no ``sub_label`` at all. That is the normal case once external
    identification is enabled, and it exercises a completely different ingest path (the
    row is stored as pending and handed to aviary-id rather than announced), so it needs
    covering here.
    """
    obj = {
        "id": event_id,
        "camera": camera,
        "label": "bird",
        "sub_label": species,
        "top_score": score,
        "score": score,
        "start_time": ts,
        "end_time": ts + 8,
        "has_clip": True,
        "has_snapshot": True,
    }
    return {"type": msg_type, "before": obj, "after": obj}


def birdnet_event(det_id: int, common: str, sci: str, code: str, conf: float, ts: float) -> dict:
    lt = time.localtime(ts)
    return {
        # Current BirdNET-Go publishes the DB id as camelCase `detectionId`
        # (older builds sent `ID`, which was always 0 over MQTT).
        "detectionId": det_id,
        "SourceNode": "BirdNET-Go",
        "Date": time.strftime("%Y-%m-%d", lt),
        "Time": time.strftime("%H:%M:%S", lt),
        "BeginTime": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(ts)),
        "EndTime": "0001-01-01T00:00:00Z",
        "SpeciesCode": code,
        "ScientificName": sci,
        "CommonName": common,
        "Confidence": conf,
        "ClipName": f"{code}_{int(ts)}.wav",
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("MQTT_HOST", "localhost"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("MQTT_PORT", "1883")))
    args = ap.parse_args()

    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    user = os.environ.get("MQTT_USER")
    if user:
        client.username_pw_set(user, os.environ.get("MQTT_PASSWORD", ""))
    client.connect(args.host, args.port, 60)
    client.loop_start()

    now = time.time()
    frigate_samples = [
        frigate_event("evt-1001", "Northern Cardinal", "feeder_cam", 0.91, now - 3600),
        frigate_event("evt-1002", "Blue Jay", "feeder_cam", 0.86, now - 7200),
        frigate_event("evt-1003", "American Goldfinch", "yard_cam", 0.78, now - 90000),
        # No sub_label: what Frigate sends with its bird classification disabled. With
        # identify_enabled on, this should be stored as pending and sent to aviary-id;
        # with it off, it should be dropped by the ignore_unclassified gate.
        frigate_event("evt-1004", None, "feeder_cam", 0.88, now - 600),
        # The in-progress message for the same shape. It must NOT trigger identification —
        # only the 'end' message does, or every event would cost several GPU passes.
        frigate_event("evt-1005", None, "feeder_cam", 0.83, now - 300, msg_type="new"),
    ]
    birdnet_samples = [
        birdnet_event(1, "Rainbow Lorikeet", "Trichoglossus moluccanus", "railor5", 0.88, now - 1800),
        birdnet_event(2, "House Sparrow", "Passer domesticus", "houspa", 0.72, now - 5400),
        birdnet_event(3, "Northern Cardinal", "Cardinalis cardinalis", "norcar", 0.95, now - 100000),
    ]

    for e in frigate_samples:
        client.publish(FRIGATE_TOPIC, json.dumps(e), qos=0)
        label = e["after"]["sub_label"] or "(no sub_label)"
        print(f"→ {FRIGATE_TOPIC}: {label} [{e['type']}]")
    for e in birdnet_samples:
        client.publish(BIRDNET_TOPIC, json.dumps(e), qos=0)
        print(f"→ {BIRDNET_TOPIC}: {e['CommonName']}")

    time.sleep(1)  # let the network loop flush
    client.loop_stop()
    client.disconnect()
    print("Done.")


if __name__ == "__main__":
    main()
