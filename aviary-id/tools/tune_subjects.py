#!/usr/bin/env python3
"""Tune the subject partition (SUBJECT_*) against real two-bird events.

Finds Frigate bird events that overlapped another event on the same camera (two birds in
view at once), asks the service to identify each with ``debug: true`` — which returns every
crop's geometry, embedding and per-crop top-1 — and then re-runs the partition locally over
a grid of similarity thresholds, printing how the crops split at each setting so you can
judge by eye whether the two birds came apart cleanly.

Usage (from the aviary-id directory, so ``app.subjects`` imports):

    pip install -r requirements-dev.txt
    python tools/tune_subjects.py --service http://10.10.69.8:8100 --token "$AVIARY_ID_TOKEN" \\
        --frigate http://10.10.69.8:5000 --camera birdzone --overlap --limit 10

    python tools/tune_subjects.py --service ... --event 1788904637.717787-wf7zvn   # one event

No GPU needed here: the service does the encoding, this script only re-clusters.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.subjects import CropMeta, partition  # noqa: E402


def get_json(url: str, timeout: float = 30.0):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def post_json(url: str, payload: dict, headers: dict, timeout: float = 240.0):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def overlapping_events(frigate: str, camera: str | None, limit: int, hours: float) -> list[dict]:
    """Bird events that shared their camera with another bird event at the same time."""
    params = f"labels=bird&limit=2000&has_clip=1&after={time.time() - hours * 3600:.0f}"
    if camera:
        params += f"&cameras={camera}"
    events = get_json(f"{frigate}/api/events?{params}")
    events = [e for e in events if e.get("end_time")]
    events.sort(key=lambda e: e["start_time"])
    picked: list[dict] = []
    for i, e in enumerate(events):
        for f in events[i + 1:]:
            if f["camera"] != e["camera"]:
                continue
            if f["start_time"] >= e["end_time"]:
                break
            # Genuine co-presence, not a tracker hand-off: require a second of overlap.
            if min(e["end_time"], f["end_time"]) - f["start_time"] >= 1.0:
                picked.append(e)
                break
        if len(picked) >= limit:
            break
    return picked


def decode(b64: str) -> np.ndarray:
    v = np.frombuffer(base64.b64decode(b64), dtype=np.float16).astype(np.float32)
    n = np.linalg.norm(v)
    return v / n if n else v


def show(crops: list[dict], subjects, label: str) -> None:
    owner = {}
    for s_idx, s in enumerate(subjects):
        for i in s.indices:
            owner[i] = s_idx
    print(f"    {label}")
    for s_idx, s in enumerate(subjects):
        kind = "primary" if s.primary else f"other {s_idx}"
        anch = "" if s.anchored else " (unanchored)"
        print(f"      [{kind}{anch}]")
        for i in s.indices:
            c = crops[i]
            geo = ""
            if c.get("center"):
                geo = f" @({c['center'][0]:.2f},{c['center'][1]:.2f})"
            if c.get("anchor_dist") is not None:
                geo += f" path±{c['anchor_dist']:.2f}"
            print(f"        {c['origin']:<16} det={c['det_score']:.2f}{geo}  "
                  f"{c.get('top1', '')} {c.get('top1_score', 0) * 100:.0f}%")
    dropped = [i for i in range(len(crops)) if i not in owner]
    if dropped:
        print("      [dropped] " + ", ".join(crops[i]["origin"] for i in dropped))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--service", default=os.environ.get("SERVICE_URL", "http://localhost:8100"))
    ap.add_argument("--token", default=os.environ.get("AVIARY_ID_TOKEN", ""))
    ap.add_argument("--frigate", default=os.environ.get("FRIGATE_URL", ""))
    ap.add_argument("--camera", default=None)
    ap.add_argument("--overlap", action="store_true",
                    help="pick events that overlapped another bird event on the same camera")
    ap.add_argument("--hours", type=float, default=72, help="how far back --overlap looks")
    ap.add_argument("--event", action="append", default=[], help="specific event id(s)")
    ap.add_argument("--limit", type=int, default=8)
    ap.add_argument("--grid", default="0.65,0.70,0.75,0.80,0.85,0.90",
                    help="comma-separated SUBJECT_SIM_MERGE values to try")
    args = ap.parse_args()

    service = args.service.rstrip("/")
    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    health = get_json(f"{service}/healthz", timeout=10)
    if not health.get("ok"):
        print(f"Service not ready: {health}", file=sys.stderr)
        return 2
    frigate = (args.frigate or health.get("frigate_url") or "").rstrip("/")

    event_ids = list(args.event)
    if args.overlap:
        if not frigate:
            print("--overlap needs --frigate (or FRIGATE_URL on the service).", file=sys.stderr)
            return 2
        found = overlapping_events(frigate, args.camera, args.limit, args.hours)
        print(f"{len(found)} event(s) overlapped another bird event on the same camera.")
        event_ids += [e["id"] for e in found]
    if not event_ids:
        print("Nothing to do: pass --event or --overlap.", file=sys.stderr)
        return 1

    grid = [float(x) for x in args.grid.split(",") if x.strip()]
    for eid in event_ids:
        try:
            res = post_json(f"{service}/identify", {"event_id": eid, "debug": True}, headers)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print(f"\n{eid}: ERROR {exc}")
            continue
        crops = (res.get("debug") or {}).get("crops") or []
        print(f"\n{'=' * 78}\n{eid}: {res.get('status')} {res.get('common_name')} "
              f"{(res.get('score') or 0) * 100:.0f}%  ({len(crops)} crops, "
              f"{len(res.get('subjects') or [])} subject(s) at service settings)")
        if not crops or not all(c.get("embedding") for c in crops):
            print("  no debug crops in the response (service older than 0.10.0?)")
            continue
        feats = np.stack([decode(c["embedding"]) for c in crops])
        sims = feats @ feats.T
        np.fill_diagonal(sims, np.nan)
        print(f"  pairwise cosine: min {np.nanmin(sims):.2f}  median {np.nanmedian(sims):.2f}  "
              f"max {np.nanmax(sims):.2f}")
        metas = [CropMeta(origin=c["origin"], det_score=c["det_score"], rank=c["rank"],
                          pre_cropped=c["pre_cropped"],
                          center=tuple(c["center"]) if c.get("center") else None,
                          anchor_dist=c.get("anchor_dist"), t=c.get("t")) for c in crops]
        svc = (res.get("debug") or {}).get("settings") or {}
        for merge in grid:
            subs = partition(metas, feats, sim_merge=merge,
                             sim_split=svc.get("sim_split", 0.55),
                             min_secondary_crops=svc.get("min_crops", 2),
                             single_crop_det=svc.get("single_det", 0.5),
                             max_subjects=svc.get("max_subjects", 3))
            show(crops, subs, f"SUBJECT_SIM_MERGE={merge:.2f} -> {len(subs)} subject(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
