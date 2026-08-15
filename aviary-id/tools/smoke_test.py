#!/usr/bin/env python3
"""Replay real Frigate bird events through aviary-id and report what it would do.

You do not have to wait for a bird. Frigate has been recording every `bird` event all
along — Aviary discarded the unclassified ones, but Frigate kept them, with clips and
snapshots. This pulls those events straight from Frigate and runs them through the
identifier, so you can judge accuracy against footage you can check by eye.

The threshold table at the end is the point of the exercise. Aviary's shipped defaults
(identify_min_score 0.35, identify_min_margin 0.08) are placeholders chosen without any
data; this shows what they would actually accept on YOUR cameras, and what you would be
sending to the review queue.

Stdlib only, so it runs anywhere python3 does — no venv, no pip.

    python3 smoke_test.py --frigate http://frigate.lan:5000 --service http://localhost:8100
    python3 smoke_test.py --limit 50 --camera feeder_cam --token "$AVIARY_ID_TOKEN"
    python3 smoke_test.py --image ~/photos/known-chickadee.jpg

With --labels the sweep becomes an actual accuracy measurement. Hand-label 30-50 events
you can identify by eye into a JSON file of {"event id": "Common Name", ...}; the run
then reports top-1/top-5 accuracy and, per threshold pair, how much of what would be
auto-accepted is actually right. That is the file to re-run after changing LABEL_FORMAT,
the detector backend, or any threshold — it turns "feels better" into a number.

    python3 smoke_test.py --limit 50 --labels my-birds.json
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


def get_json(url: str, headers: dict | None = None, timeout: float = 30.0):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def post_json(url: str, payload: dict, headers: dict | None = None, timeout: float = 180.0):
    body = json.dumps(payload).encode()
    hdrs = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def post_image(url: str, path: str, headers: dict | None = None, timeout: float = 180.0):
    """Minimal multipart upload — avoids a requests dependency for one call."""
    boundary = uuid.uuid4().hex
    filename = os.path.basename(path)
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    with open(path, "rb") as f:
        payload = f.read()
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode(),
        f"Content-Type: {ctype}\r\n\r\n".encode(),
        payload,
        f"\r\n--{boundary}--\r\n".encode(),
    ])
    hdrs = {"Content-Type": f"multipart/form-data; boundary={boundary}", **(headers or {})}
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def fetch_events(frigate: str, limit: int, camera: str | None) -> list[dict]:
    """Recent bird events. Same endpoint Aviary's own backfill uses, so it is known good."""
    params = {"labels": "bird", "limit": str(limit), "include_thumbnails": "0"}
    if camera:
        params["cameras"] = camera
    url = f"{frigate}/api/events?{urllib.parse.urlencode(params)}"
    events = get_json(url)
    # Only events with media are usable: with no clip and no snapshot there is nothing to
    # look at, and the service would correctly answer no_media every time.
    return [e for e in events if e.get("has_clip") or e.get("has_snapshot")]


def pct(value) -> str:
    return "  —  " if value is None else f"{value * 100:5.1f}%"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--service", default=os.environ.get("SERVICE_URL", "http://localhost:8100"))
    ap.add_argument("--frigate", default=os.environ.get("FRIGATE_URL", ""),
                    help="Frigate base URL. Defaults to whatever the service is configured with.")
    ap.add_argument("--token", default=os.environ.get("AVIARY_ID_TOKEN", ""))
    ap.add_argument("--limit", type=int, default=20, help="events to test (default 20)")
    ap.add_argument("--camera", default=None, help="restrict to one Frigate camera")
    ap.add_argument("--image", default=None, help="identify a single local image instead")
    ap.add_argument("--labels", default=None,
                    help='JSON file of {"event id": "Common Name"} ground truth; '
                         "adds top-1/top-5 accuracy and a correctness column to the sweep")
    ap.add_argument("--verbose", action="store_true", help="show the per-frame breakdown")
    args = ap.parse_args()

    labels: dict[str, str] = {}
    if args.labels:
        with open(args.labels, encoding="utf-8") as f:
            labels = {str(k): str(v).strip().lower() for k, v in json.load(f).items()}

    service = args.service.rstrip("/")
    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}

    # --- health first: everything else is meaningless if it came up on the CPU ---------
    try:
        health = get_json(f"{service}/healthz", timeout=10)
    except (urllib.error.URLError, OSError) as exc:
        print(f"Cannot reach the service at {service}: {exc}", file=sys.stderr)
        return 2
    if not health.get("ok"):
        print(f"Service is not ready yet (still loading the model?): {health}", file=sys.stderr)
        return 2

    print(f"Service   : {service}")
    print(f"Device    : {health.get('device')}   (cuda={health.get('cuda')})")
    print(f"Vocabulary: {health.get('species_count')} species from {health.get('species_source')}")
    if not health.get("cuda") and not health.get("cpu_only"):
        print("\n  !! Running on CPU with CPU_ONLY unset. Check nvidia-container-toolkit and\n"
              "     that torch came from the cu126 index — cu128/cu129 have no Pascal support.\n")
    if health.get("species_source") == "bundled fallback":
        print("\n  !! Using the bundled fallback species list. Set EBIRD_API_KEY and EBIRD_REGION\n"
              "     for a regional list — it is the single biggest accuracy lever here.\n")

    # --- single image mode ------------------------------------------------------------
    if args.image:
        result = post_image(f"{service}/identify/image", args.image, headers)
        print(f"\n{args.image}")
        print(f"  {result.get('common_name')} ({result.get('scientific_name')})")
        print(f"  score {pct(result.get('score'))}  margin {pct(result.get('margin'))}"
              f"  runner-up {result.get('runner_up')}")
        return 0

    frigate = (args.frigate or health.get("frigate_url") or "").rstrip("/")
    if not frigate:
        print("No Frigate URL — pass --frigate or set FRIGATE_URL on the service.", file=sys.stderr)
        return 2

    print(f"Frigate   : {frigate}\n")
    try:
        events = fetch_events(frigate, args.limit, args.camera)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"Could not list Frigate events: {exc}", file=sys.stderr)
        return 2
    if not events:
        print("No bird events with media found in Frigate.", file=sys.stderr)
        return 1

    print(f"Testing {len(events)} event(s). Frigate's own label is shown for comparison;\n"
          f"'—' means Frigate's classifier produced nothing (expected once it is off).\n")

    header = (f"{'event':<26} {'camera':<14} {'frigate':<22} {'aviary-id':<24} "
              f"{'score':>7} {'margin':>7} {'fr':>3} {'ms':>6}")
    print(header)
    print("-" * len(header))

    results = []
    for event in events:
        eid = event["id"]
        started = time.monotonic()
        try:
            res = post_json(f"{service}/identify", {"event_id": eid}, headers)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print(f"{eid:<26} {'':<14} {'':<22} ERROR: {exc}")
            continue
        elapsed = res.get("elapsed_ms") or int((time.monotonic() - started) * 1000)
        res["_event_id"] = eid
        results.append(res)

        theirs = event.get("sub_label") or "—"
        if isinstance(theirs, (list, tuple)):
            theirs = theirs[0] if theirs else "—"
        ours = res.get("common_name") or f"({res.get('status')})"
        if not res.get("localized", True):
            ours += " ~"  # classified uncropped; the detector found no bird
        when = time.strftime("%m-%d %H:%M", time.localtime(event.get("start_time", 0)))
        print(f"{eid[:24]:<26} {(event.get('camera') or '')[:13]:<14} {str(theirs)[:21]:<22} "
              f"{ours[:23]:<24} {pct(res.get('score')):>7} {pct(res.get('margin')):>7} "
              f"{res.get('frames_used', 0):>3} {elapsed:>6}   {when}")

        if args.verbose:
            for frame in res.get("per_frame", []):
                trained = ""
                if frame.get("trained_top1"):
                    trained = (f"  |  trained: {frame['trained_top1']} "
                               f"{frame.get('trained_score', 0) * 100:.0f}%")
                elif res.get("trained"):
                    trained = "  |  trained: saw nothing"
                print(f"    {frame['origin']:<18} det={frame['det_score']:.2f}  "
                      f"{frame['top1']} {frame['top1_score'] * 100:.0f}%  /  "
                      f"{frame['top2']} {frame['top2_score'] * 100:.0f}%{trained}")

    ok = [r for r in results if r.get("status") == "ok"]
    if not ok:
        print("\nNo events produced a result. Check the service logs.")
        return 1

    print(f"\n{'=' * 78}")
    print(f"{len(ok)}/{len(events)} events identified. "
          f"{sum(1 for r in ok if not r.get('localized', True))} were classified uncropped "
          f"(detector found no bird).")

    # --- accuracy against hand labels ---------------------------------------------------
    def top1_correct(r) -> bool:
        return (r.get("common_name") or "").strip().lower() == labels.get(r["_event_id"])

    def top5_correct(r) -> bool:
        truth = labels.get(r["_event_id"])
        return any((c.get("common_name") or "").strip().lower() == truth
                   for c in (r.get("candidates") or [])[:5])

    labelled = [r for r in ok if r.get("_event_id") in labels] if labels else []
    if labels:
        skipped = len(labels) - len(labelled)
        if labelled:
            t1 = sum(map(top1_correct, labelled))
            t5 = sum(map(top5_correct, labelled))
            print(f"\nAccuracy on {len(labelled)} labelled event(s): "
                  f"top-1 {t1}/{len(labelled)} ({t1 / len(labelled) * 100:.0f}%), "
                  f"top-5 {t5}/{len(labelled)} ({t5 / len(labelled) * 100:.0f}%)."
                  + (f" {skipped} label(s) matched no tested event." if skipped else ""))
            wrong = [r for r in labelled if not top1_correct(r)]
            for r in wrong[:10]:
                print(f"  wrong: {r['_event_id'][:24]:<26} said "
                      f"{r.get('common_name')!r}, truth {labels[r['_event_id']]!r} "
                      f"(score {pct(r.get('score'))}, margin {pct(r.get('margin'))})")
        else:
            print("\nNo tested event matched an entry in the labels file — "
                  "check the event ids.")

    # --- threshold table --------------------------------------------------------------
    # The reason this script exists. What this shows is the cost of each threshold pair —
    # how much you would accept, and how much you would push into the review queue. With
    # --labels it also shows how much of the accepted set is actually right, which is the
    # number to tune on.
    print("\nWhat each threshold pair would accept, on this sample:\n")
    correct_col = f" {'correct':>10}" if labelled else ""
    print(f"  {'min_score':>10} {'min_margin':>11} {'accepted':>10} {'to review':>11}"
          f"{correct_col}")
    for min_score, min_margin in ((0.20, 0.05), (0.35, 0.08), (0.50, 0.15),
                                  (0.60, 0.25), (0.75, 0.40)):
        accepted = [r for r in ok
                    if (r.get("score") or 0) >= min_score
                    and (r.get("margin") or 0) >= min_margin]
        passed = len(accepted)
        extra = ""
        if labelled:
            acc_labelled = [r for r in accepted if r.get("_event_id") in labels]
            if acc_labelled:
                good = sum(map(top1_correct, acc_labelled))
                extra = f" {good:>4}/{len(acc_labelled):<3} ({good / len(acc_labelled) * 100:3.0f}%)"
            else:
                extra = f" {'—':>10}"
        marker = "   <- shipped default" if (min_score, min_margin) == (0.35, 0.08) else ""
        print(f"  {min_score:>10.2f} {min_margin:>11.2f} "
              f"{passed:>7} ({passed / len(ok) * 100:3.0f}%) {len(ok) - passed:>8}"
              f"{extra}{marker}")

    scores = sorted(r.get("score") or 0 for r in ok)
    margins = sorted(r.get("margin") or 0 for r in ok)
    mid = len(scores) // 2
    print(f"\n  median score {scores[mid] * 100:.0f}%, median margin {margins[mid] * 100:.0f}%")
    print("\nPick thresholds by checking the names above against the footage, not by these\n"
          "percentages alone — a setting that accepts everything confidently and wrongly\n"
          "looks identical here to one that is right.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
