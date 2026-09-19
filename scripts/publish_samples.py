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
FRIGATE_UPDATE_TOPIC = os.environ.get("FRIGATE_UPDATE_TOPIC", "frigate/tracked_object_update")
BIRDNET_TOPIC = os.environ.get("BIRDNET_TOPIC", "birdnet")


def frigate_event(event_id: str, species: Optional[str], camera: str, score: float,
                  ts: float, msg_type: str = "end",
                  species_score: Optional[float] = None) -> dict:
    """A Frigate event message.

    ``species=None`` is the shape Frigate publishes when its own bird classification is
    turned off — no ``sub_label`` at all. With identification enabled that exercises the
    full-identification path (the row is stored as pending and handed to aviary-id).

    ``species_score`` gives the ``[name, score]`` pair shape current Frigate publishes
    when its classifier IS on: the score is the classifier's confidence in the name.
    With identification enabled and aviary-id 0.12.0+, such an event is held for
    confirmation (stored ``confirming``, announced once aviary-id has looked at Frigate's
    crop) rather than announced at ``end``.
    """
    obj = {
        "id": event_id,
        "camera": camera,
        "label": "bird",
        "sub_label": [species, species_score] if (species and species_score is not None) else species,
        "top_score": score,
        "score": score,
        "start_time": ts,
        "end_time": ts + 8 if msg_type == "end" else None,
        "has_clip": True,
        "has_snapshot": True,
    }
    return {"type": msg_type, "before": obj, "after": obj}


def frigate_classification(event_id: str, species: str, score: float, camera: str,
                           ts: float) -> dict:
    """A ``frigate/tracked_object_update`` classification message — how a label Frigate
    applies AFTER an event's end message travels (needs ``frigate_object_update_topic``)."""
    return {"type": "classification", "id": event_id, "camera": camera, "timestamp": ts,
            "model": "bird", "sub_label": species, "score": score}


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
        # Frigate's classifier ON, as current Frigate publishes it: a [name, score] pair.
        # The bird arrives as plain 'bird' first and gains the label on a later message —
        # here on the end. With aviary-id 0.12.0+ the end is stored 'confirming' and the
        # announcement waits for the confirmation; nothing is announced twice.
        frigate_event("evt-1006", None, "feeder_cam", 0.85, now - 240, msg_type="new"),
        frigate_event("evt-1006", "Northern Cardinal", "feeder_cam", 0.85, now - 240,
                      msg_type="update", species_score=0.91),
        frigate_event("evt-1006", "Northern Cardinal", "feeder_cam", 0.88, now - 240,
                      species_score=0.93),
        # Ended as plain 'bird' (full identification); the label lands afterwards on
        # the tracked_object_update topic — see late_samples below.
        frigate_event("evt-1007", None, "feeder_cam", 0.87, now - 120),
    ]
    late_samples = [
        frigate_classification("evt-1007", "Carolina Wren", 0.95, "feeder_cam", now - 110),
    ]
    birdnet_samples = [
        birdnet_event(1, "Rainbow Lorikeet", "Trichoglossus moluccanus", "railor5", 0.88, now - 1800),
        birdnet_event(2, "House Sparrow", "Passer domesticus", "houspa", 0.72, now - 5400),
        birdnet_event(3, "Northern Cardinal", "Cardinalis cardinalis", "norcar", 0.95, now - 100000),
    ]

    for e in frigate_samples:
        client.publish(FRIGATE_TOPIC, json.dumps(e), qos=0)
        label = e["after"]["sub_label"] or "(no sub_label)"
        if isinstance(label, list):
            label = f"{label[0]} ({label[1]:.2f})"
        print(f"→ {FRIGATE_TOPIC}: {e['after']['id']} {label} [{e['type']}]")
    time.sleep(0.5)  # the late label is late
    for e in late_samples:
        client.publish(FRIGATE_UPDATE_TOPIC, json.dumps(e), qos=0)
        print(f"→ {FRIGATE_UPDATE_TOPIC}: {e['id']} {e['sub_label']} ({e['score']:.2f}) "
              f"[classification]")
    for e in birdnet_samples:
        client.publish(BIRDNET_TOPIC, json.dumps(e), qos=0)
        print(f"→ {BIRDNET_TOPIC}: {e['CommonName']}")

    time.sleep(1)  # let the network loop flush
    client.loop_stop()
    client.disconnect()
    print("Done.")


if __name__ == "__main__":
    main()
