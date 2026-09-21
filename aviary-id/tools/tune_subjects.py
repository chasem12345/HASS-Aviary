#!/usr/bin/env python3
"""Tune the subject partition (SUBJECT_*) against real two-bird events.

Finds Frigate bird events that overlapped another event on the same camera (two birds in
view at once), asks the service to identify each with ``debug: true`` — which returns every
crop's geometry, embedding and per-crop top-1 — and then re-runs the partition locally over
a grid of thresholds, printing how the crops split at each setting so you can judge by eye
whether the two birds came apart cleanly.

Usage (from the aviary-id directory, so ``app.subjects`` imports):

    pip install -r requirements-dev.txt
    python tools/tune_subjects.py --service http://<aviary-id-host>:8100 --token "$AVIARY_ID_TOKEN" \
        --frigate http://<frigate-host>:5000 --camera birdzone --overlap --zone bath --limit 10 \
        --save tune-out/

    python tools/tune_subjects.py --service ... --event 1788904637.717787-wf7zvn   # one event
    python tools/tune_subjects.py --recent 15 --save tune-out/       # single-bird controls
    python tools/tune_subjects.py --replay tune-out/ --axis sim_primary --grid 0.75,0.80,0.85,0.90

``--save`` keeps each response (minus the JPEGs) so ``--replay`` can re-partition offline
with no GPU calls — the same folder doubles as a regression corpus. ``--set key=value``
pins any knob while ``--axis``/``--grid`` sweep another. If Aviary sends a PTZ window
(``identify_zoom_map``), pass ``--zoom-camera``/``--zoom-offset`` so the service sees the
same crops it does in production; zoomed frames carry no path anchors, which is exactly
the case the primary bar exists for.

``SERVICE_URL``, ``FRIGATE_URL`` and ``AVIARY_ID_TOKEN`` in the environment stand in for the
flags. No GPU needed here: the service does the encoding, this script only re-clusters.
"""

from __future__ import annotations

import argparse
import base64
import glob
import json
import os
import sys
import time
import urllib.error
import urllib.request

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.subjects import CropMeta, partition  # noqa: E402

# Tool knob name -> partition() keyword. The service's debug.settings uses the tool names.
KNOBS = {
    "sim_merge": "sim_merge", "sim_split": "sim_split", "sim_primary": "sim_primary",
    "sim_secondary": "sim_secondary", "min_crops": "min_secondary_crops",
    "single_det": "single_crop_det", "max_subjects": "max_subjects",
    "vote_min": "vote_min", "sim_cross": "sim_cross",
}
DEFAULT_GRID = {
    "sim_merge": "0.65,0.70,0.75,0.80,0.85,0.90",
    "sim_primary": "0.75,0.80,0.85,0.90",
    "sim_secondary": "0.55,0.60,0.65,0.70,0.75",
    "sim_split": "0.45,0.50,0.55,0.60,0.65",
    "single_det": "0.3,0.4,0.5,0.6,0.7",
    "min_crops": "1,2,3",
    "max_subjects": "2,3,4",
    "vote_min": "0.3,0.4,0.5,0.6,0.7",
    "sim_cross": "0.80,0.85,0.90,0.95",
}


def get_json(url: str, timeout: float = 30.0):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def post_json(url: str, payload: dict, headers: dict, timeout: float = 240.0):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def list_events(frigate: str, camera: str | None, hours: float, zone: str | None) -> list[dict]:
    """Ended bird events with clips, oldest first, optionally in one camera / one zone."""
    # has_clip is filtered here rather than with Frigate's query flag: on 0.18 the flag
    # makes /api/events answer 500.
    params = f"labels=bird&limit=2000&after={time.time() - hours * 3600:.0f}"
    if camera:
        params += f"&cameras={camera}"
    events = [e for e in get_json(f"{frigate}/api/events?{params}")
              if e.get("end_time") and e.get("has_clip")]
    if zone:
        events = [e for e in events
                  if zone.lower() in {z.lower() for z in (e.get("zones") or [])}]
    events.sort(key=lambda e: e["start_time"])
    return events


def overlapping_ids(events: list[dict], min_overlap: float) -> set[str]:
    """Ids of events that shared their camera with another bird event at the same time."""
    out: set[str] = set()
    for i, e in enumerate(events):
        for f in events[i + 1:]:
            if f["camera"] != e["camera"]:
                continue
            if f["start_time"] >= e["end_time"]:
                break
            # Genuine co-presence, not a tracker hand-off: require real overlap.
            if min(e["end_time"], f["end_time"]) - f["start_time"] >= min_overlap:
                out.add(e["id"])
                out.add(f["id"])
    return out


def event_zoom(frigate: str, event_id: str, camera: str, offset: float) -> dict | None:
    """The PTZ recordings window Aviary would send for this event (identify_zoom_map)."""
    try:
        ev = get_json(f"{frigate}/api/events/{event_id}")
    except (urllib.error.URLError, OSError, ValueError):
        return None
    start, end = ev.get("start_time"), ev.get("end_time")
    if not start or not end:
        return None
    return {"camera": camera, "start": start + offset, "end": end}


def decode(b64: str) -> np.ndarray:
    v = np.frombuffer(base64.b64decode(b64), dtype=np.float16).astype(np.float32)
    n = np.linalg.norm(v)
    return v / n if n else v


def metas_from(crops: list[dict], footage: str | None = None) -> list[CropMeta]:
    """CropMeta per debug crop; top-1 names become small ints so the partition can compare.

    Captures from a service older than 0.13.0 lack ``zoomed`` and ``frame_boxes``; they are
    inferred — a clip crop with no anchor in a ``footage == "zoom"`` capture was zoomed, and
    a frame's box count is the number of classified crops sharing its origin (an
    undercount: the service forwards at most a few boxes per frame).
    """
    names = sorted({c.get("top1") or "" for c in crops})
    index = {n: i for i, n in enumerate(names)}
    per_origin: dict[str, int] = {}
    for c in crops:
        per_origin[c["origin"]] = per_origin.get(c["origin"], 0) + 1
    out = []
    for c in crops:
        is_clip = c["origin"].startswith("clip@")
        zoomed = c.get("zoomed")
        if zoomed is None:
            zoomed = footage == "zoom" and is_clip and c.get("anchor_dist") is None
        out.append(CropMeta(
            origin=c["origin"], det_score=c["det_score"], rank=c["rank"],
            pre_cropped=c["pre_cropped"],
            center=tuple(c["center"]) if c.get("center") else None,
            anchor_dist=c.get("anchor_dist"), t=c.get("t"),
            top1=index[c.get("top1") or ""] if c.get("top1") else None,
            top1_score=float(c.get("top1_score") or 0.0) if c.get("top1") else 1.0,
            zoomed=bool(zoomed),
            frame_boxes=int(c.get("frame_boxes") or per_origin[c["origin"]]),
        ))
    return out


def crop_line(c: dict) -> str:
    geo = ""
    if c.get("center"):
        geo = f" @({c['center'][0]:.2f},{c['center'][1]:.2f})"
    if c.get("anchor_dist") is not None:
        geo += f" path±{c['anchor_dist']:.2f}"
    pre = " [frigate]" if c.get("pre_cropped") else ""
    zoom = " zoom" if c.get("zoomed") else ""
    boxes = f" boxes={c['frame_boxes']}" if c.get("frame_boxes", 1) != 1 else ""
    return (f"{c['origin']:<16} det={c['det_score']:.2f}{geo}{pre}{zoom}{boxes}  "
            f"{c.get('top1', '')} {c.get('top1_score', 0) * 100:.0f}%")


def show(crops: list[dict], subjects, label: str) -> None:
    print(f"    {label}")
    for s_idx, s in enumerate(subjects):
        if not s.kept:
            kind = "dropped"
        elif s.primary:
            kind = "primary"
        else:
            kind = f"other {s_idx}"
        anch = "" if s.anchored else " (unanchored)"
        coh = f" cohesion {s.cohesion:.2f}" if s.cohesion is not None else ""
        cooc = f" beside-primary {s.co_occurring}" if not s.primary and s.kept else ""
        solo = f" solo {s.solo_frames}" if s.solo_frames else ""
        print(f"      [{kind}{anch}{coh}{cooc}{solo}]")
        for i, note in zip(s.indices, s.notes):
            print(f"        {crop_line(crops[i])}   {note}")


def matrix(crops: list[dict], feats: np.ndarray) -> None:
    """The n×n cosines, plus same-top-1 vs different-top-1 pair summaries.

    The second part is the direct answer to "is SUBJECT_SIM_MERGE too permissive for the
    primary": any pair that disagrees on the species yet sits above the bar is a pair the
    partition would happily call one bird.
    """
    n = len(crops)
    sims = feats @ feats.T
    if n <= 12:
        print("  cosine matrix (rows/cols in crop order):")
        head = "      " + " ".join(f"{j:>5}" for j in range(n))
        print(head)
        for i in range(n):
            row = " ".join(f"{sims[i, j]:5.2f}" if i != j else "   · " for j in range(n))
            print(f"  {i:>3} {row}   {crop_line(crops[i])}")
    agree, differ = [], []
    for i in range(n):
        for j in range(i + 1, n):
            same = crops[i].get("top1") and crops[i].get("top1") == crops[j].get("top1")
            (agree if same else differ).append(float(sims[i, j]))

    def summary(vals: list[float]) -> str:
        if not vals:
            return "n/a"
        return f"min {min(vals):.2f}  median {float(np.median(vals)):.2f}  max {max(vals):.2f}"
    print(f"  pairs agreeing on top-1 ({len(agree)}):  {summary(agree)}")
    print(f"  pairs differing on top-1 ({len(differ)}): {summary(differ)}")


def service_summary(res: dict) -> None:
    for s in res.get("subjects") or []:
        kind = "primary" if s.get("primary") else f"other {s.get('idx')}"
        anch = "" if s.get("anchored", True) else " (unanchored)"
        pur = f" purity {s['purity']:.2f}" if s.get("purity") is not None else ""
        coh = f" cohesion {s['cohesion']:.2f}" if s.get("cohesion") is not None else ""
        tgt = f" target={s['embedding_target']!r}" if s.get("embedding_target") else ""
        best = f" best={s['best_origin']}" if s.get("best_origin") else ""
        cooc = (f" beside-primary {s['co_occurring']}"
                if not s.get("primary") and s.get("co_occurring") is not None else "")
        print(f"  [{kind}{anch}] {s.get('common_name')} {(s.get('score') or 0) * 100:.0f}% "
              f"on {s.get('n_frames', 0)} crop(s){pur}{coh}{cooc}{best}{tgt}")
    for f in res.get("unassigned") or []:
        print(f"  [unassigned] {f['origin']:<16} det={f['det_score']:.2f}  "
              f"{f['top1']} {f['top1_score'] * 100:.0f}%")


def strip_images(res: dict) -> dict:
    out = dict(res)
    out.pop("best_crop", None)
    out["subjects"] = [{k: v for k, v in s.items() if k != "best_crop"}
                       for s in res.get("subjects") or []]
    return out


def load_saved(folder: str) -> list[tuple[str, dict]]:
    items = []
    for path in sorted(glob.glob(os.path.join(folder, "*.json"))):
        with open(path, encoding="utf-8") as f:
            items.append((os.path.splitext(os.path.basename(path))[0], json.load(f)))
    return items


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--service", default=os.environ.get("SERVICE_URL", "http://localhost:8100"))
    ap.add_argument("--token", default=os.environ.get("AVIARY_ID_TOKEN", ""))
    ap.add_argument("--frigate", default=os.environ.get("FRIGATE_URL", ""))
    ap.add_argument("--camera", default=None)
    ap.add_argument("--zone", default=None, help="only events that touched this Frigate zone")
    ap.add_argument("--overlap", action="store_true",
                    help="pick events that overlapped another bird event on the same camera")
    ap.add_argument("--min-overlap", type=float, default=1.0,
                    help="seconds of co-presence --overlap requires (default 1)")
    ap.add_argument("--recent", type=int, default=0,
                    help="also pick this many recent NON-overlapping events (single-bird controls)")
    ap.add_argument("--hours", type=float, default=72, help="how far back to look")
    ap.add_argument("--event", action="append", default=[], help="specific event id(s)")
    ap.add_argument("--limit", type=int, default=8)
    ap.add_argument("--zoom-camera", default=None,
                    help="send the PTZ recordings window Aviary would (identify_zoom_map)")
    ap.add_argument("--zoom-offset", type=float, default=0.0,
                    help="seconds added to the event start for --zoom-camera")
    ap.add_argument("--target", default=None, help="request target species (0.11.0+)")
    ap.add_argument("--target-subject", type=int, default=0)
    ap.add_argument("--save", default=None, help="folder to keep each response (no JPEGs)")
    ap.add_argument("--replay", default=None, help="re-partition saved responses; no service calls")
    ap.add_argument("--axis", default="sim_merge", choices=sorted(KNOBS),
                    help="which knob --grid sweeps (default sim_merge)")
    ap.add_argument("--grid", default=None, help="comma-separated values for --axis")
    ap.add_argument("--set", action="append", default=[], metavar="KNOB=VALUE",
                    help="pin a knob for every run, e.g. --set sim_primary=0.85")
    args = ap.parse_args()

    pinned: dict[str, float] = {}
    for item in args.set:
        key, _, value = item.partition("=")
        if key not in KNOBS:
            print(f"Unknown knob {key!r}; choose from {', '.join(sorted(KNOBS))}.", file=sys.stderr)
            return 2
        pinned[key] = float(value)
    grid_src = args.grid or DEFAULT_GRID[args.axis]
    grid = [float(x) for x in grid_src.split(",") if x.strip()]

    items: list[tuple[str, dict]] = []
    if args.replay:
        items = load_saved(args.replay)
        print(f"Replaying {len(items)} saved response(s) from {args.replay}.")
    else:
        service = args.service.rstrip("/")
        headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
        health = get_json(f"{service}/healthz", timeout=10)
        if not health.get("ok"):
            print(f"Service not ready: {health}", file=sys.stderr)
            return 2
        print(f"Service {service} version {health.get('version', '<0.11.0')}")
        frigate = (args.frigate or health.get("frigate_url") or "").rstrip("/")
        event_ids = list(args.event)
        if args.overlap or args.recent:
            if not frigate:
                print("--overlap/--recent need --frigate (or FRIGATE_URL on the service).",
                      file=sys.stderr)
                return 2
            events = list_events(frigate, args.camera, args.hours, args.zone)
            overlapping = overlapping_ids(events, args.min_overlap)
            if args.overlap:
                picked = [e["id"] for e in events if e["id"] in overlapping][-args.limit:]
                print(f"{len(picked)} of {len(overlapping)} overlapping event(s) picked.")
                event_ids += picked
            if args.recent:
                solo = [e["id"] for e in events if e["id"] not in overlapping][-args.recent:]
                print(f"{len(solo)} recent single-bird control event(s) picked.")
                event_ids += solo
        if not event_ids:
            print("Nothing to do: pass --event, --overlap, --recent or --replay.", file=sys.stderr)
            return 1
        if args.save:
            os.makedirs(args.save, exist_ok=True)
        for eid in event_ids:
            payload: dict = {"event_id": eid, "debug": True}
            if args.zoom_camera and frigate:
                zoom = event_zoom(frigate, eid, args.zoom_camera, args.zoom_offset)
                if zoom:
                    payload["zoom"] = zoom
            if args.target:
                payload["target"] = args.target
                payload["target_subject"] = args.target_subject
            try:
                res = post_json(f"{service}/identify", payload, headers)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                print(f"\n{eid}: ERROR {exc}")
                continue
            if args.save:
                with open(os.path.join(args.save, f"{eid}.json"), "w", encoding="utf-8") as f:
                    json.dump(strip_images(res), f)
            items.append((eid, res))

    for eid, res in items:
        crops = (res.get("debug") or {}).get("crops") or []
        print(f"\n{'=' * 78}\n{eid}: {res.get('status')} {res.get('common_name')} "
              f"{(res.get('score') or 0) * 100:.0f}%  ({len(crops)} crops, "
              f"{len(res.get('subjects') or [])} subject(s) at service settings)")
        service_summary(res)
        if not crops or not all(c.get("embedding") for c in crops):
            print("  no debug crops in the response (service older than 0.10.0?)")
            continue
        feats = np.stack([decode(c["embedding"]) for c in crops])
        matrix(crops, feats)
        metas = metas_from(crops, footage="zoom" if (res.get("timings") or {}).get("zoom_clip")
                           else None)
        svc = (res.get("debug") or {}).get("settings") or {}
        base = {k: svc.get(k) for k in KNOBS if svc.get(k) is not None}
        base.update(pinned)
        for value in grid:
            knobs = dict(base)
            knobs[args.axis] = int(value) if args.axis in ("min_crops", "max_subjects") else value
            kwargs = {KNOBS[k]: v for k, v in knobs.items()}
            subs = partition(metas, feats, include_dropped=True, **kwargs)
            label = ", ".join(f"{k}={v}" for k, v in sorted(knobs.items()))
            show(crops, subs, f"{args.axis}={value:g}  [{label}] -> "
                              f"{sum(1 for s in subs if s.kept)} subject(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
