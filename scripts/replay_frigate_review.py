#!/usr/bin/env python3
"""Replay one Frigate review item and its events over MQTT, to exercise visits locally.

Pulls a review item (and the events it lists) from a live Frigate, then publishes the
same sequence Frigate would: the review ``new``, each event's ``new``/``end`` (shuffled,
so out-of-order arrival is covered), review ``update``s as the member list grows, and
the review ``end``. Run it twice with ``--order`` flipped to prove the link is made
whichever side lands first.

Usage:
    pip install paho-mqtt httpx
    python scripts/replay_frigate_review.py --frigate http://10.10.69.8:5000 \
        --review 1788904615.494584-jotr6i --host localhost [--order events-first] \
        [--label 0=Northern\\ Cardinal --label 3=Carolina\\ Wren]

``--label IDX=NAME`` forces a sub_label onto the IDX-th event (by start time) so the
notification path has species to announce without an identification service; two
different labels should yield exactly two announcements for the visit.

Env overrides: MQTT_HOST, MQTT_PORT, MQTT_USER, MQTT_PASSWORD, FRIGATE_TOPIC,
FRIGATE_REVIEW_TOPIC.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time

import httpx
import paho.mqtt.client as mqtt

FRIGATE_TOPIC = os.environ.get("FRIGATE_TOPIC", "frigate/events")
REVIEW_TOPIC = os.environ.get("FRIGATE_REVIEW_TOPIC", "frigate/reviews")


def fetch(frigate: str, review_id: str) -> tuple[dict, list[dict]]:
    with httpx.Client(timeout=30) as client:
        item = client.get(f"{frigate}/api/review/{review_id}").json()
        # Some Frigate versions wrap the item; unwrap defensively.
        if isinstance(item, dict) and "id" not in item and "data" in item and isinstance(item["data"], dict) and "id" in item["data"]:
            item = item["data"]
        events = []
        for ref in item["data"]["detections"]:
            resp = client.get(f"{frigate}/api/events/{ref}")
            if resp.status_code == 200:
                events.append(resp.json())
            else:
                print(f"  event {ref}: HTTP {resp.status_code} (skipped)", file=sys.stderr)
    events.sort(key=lambda e: e["start_time"])
    return item, events


def event_msg(ev: dict, kind: str, label: str | None) -> dict:
    after = {
        "id": ev["id"], "camera": ev["camera"], "label": "bird",
        "sub_label": label, "top_score": ev.get("data", {}).get("top_score") or ev.get("top_score"),
        "score": ev.get("data", {}).get("score"), "start_time": ev["start_time"],
        "end_time": ev["end_time"] if kind == "end" else None,
        "has_clip": ev.get("has_clip", False) and kind == "end",
        "has_snapshot": ev.get("has_snapshot", False),
        "entered_zones": ev.get("zones") or [], "current_zones": ev.get("zones") or [],
        "data": ev.get("data") or {},
    }
    return {"type": kind, "before": after, "after": after}


def review_msg(item: dict, kind: str, refs: list[str]) -> dict:
    after = {
        "id": item["id"], "camera": item["camera"], "start_time": item["start_time"],
        "end_time": item["end_time"] if kind == "end" else None,
        "severity": item["severity"], "thumb_path": item.get("thumb_path"),
        "data": {**item["data"], "detections": refs},
    }
    return {"type": kind, "before": after, "after": after}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frigate", required=True)
    ap.add_argument("--review", required=True)
    ap.add_argument("--host", default=os.environ.get("MQTT_HOST", "localhost"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("MQTT_PORT", "1883")))
    ap.add_argument("--user", default=os.environ.get("MQTT_USER"))
    ap.add_argument("--password", default=os.environ.get("MQTT_PASSWORD"))
    ap.add_argument("--order", choices=("review-first", "events-first"), default="review-first")
    ap.add_argument("--label", action="append", default=[], help="IDX=Species name")
    ap.add_argument("--delay", type=float, default=0.05, help="seconds between publishes")
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()

    labels: dict[int, str] = {}
    for spec in args.label:
        idx, _, name = spec.partition("=")
        labels[int(idx)] = name

    item, events = fetch(args.frigate, args.review)
    print(f"review {item['id']} on {item['camera']}: {len(events)} events")

    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id="aviary-replay")
    if args.user:
        client.username_pw_set(args.user, args.password)
    client.connect(args.host, args.port, keepalive=30)
    client.loop_start()

    def pub(topic: str, msg: dict) -> None:
        client.publish(topic, json.dumps(msg), qos=0).wait_for_publish()
        time.sleep(args.delay)

    rnd = random.Random(args.seed)
    order = list(range(len(events)))
    rnd.shuffle(order)

    # Interleave: review new, then events (shuffled) with review updates every few events,
    # then review end with the full list.
    if args.order == "review-first":
        pub(REVIEW_TOPIC, review_msg(item, "new", [events[order[0]]["id"]] if events else []))
    listed: list[str] = []
    for n, i in enumerate(order):
        ev = events[i]
        label = labels.get(i)
        pub(FRIGATE_TOPIC, event_msg(ev, "new", None))
        pub(FRIGATE_TOPIC, event_msg(ev, "update", label))
        pub(FRIGATE_TOPIC, event_msg(ev, "end", label))
        listed.append(ev["id"])
        if args.order == "events-first" and n == 0:
            pub(REVIEW_TOPIC, review_msg(item, "new", list(listed)))
        elif n % 4 == 3:
            pub(REVIEW_TOPIC, review_msg(item, "update", list(listed)))
    pub(REVIEW_TOPIC, review_msg(item, "end", [e["id"] for e in events]))
    client.loop_stop()
    client.disconnect()
    print("published; check the add-on log for the visit and its announcements")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
