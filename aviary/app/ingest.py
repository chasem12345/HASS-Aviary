"""Normalize Frigate and BirdNET-Go MQTT payloads into ``detections`` rows."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

from . import db

log = logging.getLogger("aviary.ingest")


def _now() -> float:
    return time.time()


# --------------------------------------------------------------------------- Frigate

def build_frigate_row(obj: dict) -> Optional[dict]:
    """Build a detections row from a Frigate object.

    Works for both the MQTT event ``after`` object and an object from the
    ``GET /api/events`` HTTP API (the fields we use overlap; the HTTP API also nests the
    score under ``data``). Returns ``None`` for non-bird objects or when there's no id.
    """
    if not isinstance(obj, dict) or obj.get("label") != "bird":
        return None
    event_id = obj.get("id")
    if not event_id:
        return None

    sub_label = obj.get("sub_label")
    # sub_label can be a plain string or a [name, score] pair depending on Frigate version.
    if isinstance(sub_label, (list, tuple)):
        sub_label = sub_label[0] if sub_label else None
    common_name = sub_label or "bird"

    data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
    confidence = _as_float(
        obj.get("top_score")
        or obj.get("score")
        or data.get("top_score")
        or data.get("score")
    )

    has_clip = 1 if obj.get("has_clip") else 0
    has_snapshot = 1 if obj.get("has_snapshot") else 0

    return {
        "source": "frigate",
        "source_ref": str(event_id),
        "common_name": common_name,
        "scientific_name": None,
        "species_code": None,
        "confidence": confidence,
        "location": obj.get("camera"),
        "start_time": _as_float(obj.get("start_time")) or _now(),
        "end_time": _as_float(obj.get("end_time")),
        "has_clip": has_clip,
        "has_snapshot": has_snapshot,
        # The proxy resolves these refs back to Frigate API URLs using the event id.
        "clip_ref": str(event_id) if has_clip else None,
        "snapshot_ref": str(event_id) if has_snapshot else None,
        "raw_json": json.dumps(obj)[:8000],
        "created_at": _now(),
    }


def handle_frigate(payload: bytes) -> None:
    """Handle a ``frigate/events`` message.

    Frigate publishes ``{"type": "new"|"update"|"end", "before": {...}, "after": {...}}``.
    We only track ``bird`` objects and upsert on the event id so the final species (from
    ``sub_label``) and best score win.
    """
    try:
        msg = json.loads(payload)
    except (ValueError, TypeError):
        log.warning("Frigate: could not decode payload")
        return

    after = msg.get("after") or msg.get("before") or {}
    row = build_frigate_row(after)
    if row is None:
        return
    db.upsert_detection(row)
    log.debug("Frigate detection upserted: %s (%s)", row["common_name"], row["source_ref"])


# ------------------------------------------------------------------------- BirdNET-Go

def build_birdnet_row(msg: dict) -> Optional[dict]:
    """Build a detections row from a BirdNET-Go detection.

    Expects the MQTT payload shape (PascalCase keys: ``ID``, ``CommonName``,
    ``ScientificName``, ``SpeciesCode``, ``Confidence``, ``ClipName``, ``BeginTime``,
    ``EndTime``, ``Date``, ``Time``, ``SourceNode``). The HTTP-API backfill maps its
    camelCase response into this shape first (see ``backfill.birdnet_msg_from_api``), so
    both paths produce identical ``source_ref`` values and dedupe cleanly.
    """
    if not isinstance(msg, dict):
        return None

    common_name = msg.get("CommonName") or "bird"
    start_time = _birdnet_epoch(msg) or _now()

    # BirdNET-Go's detection id may repeat across restarts; combine with the timestamp
    # to form a stable-enough dedup key while still collapsing duplicate publishes.
    raw_id = msg.get("ID")
    source_ref = f"{raw_id}-{int(start_time)}" if raw_id not in (None, 0) else str(int(start_time * 1000))

    # Audio clip locator: BirdNET-Go payloads may include a clip filename under one of a
    # few keys depending on version. Store whatever is present; the proxy resolves it
    # against birdnet_url. If none is present we simply have no clip.
    clip_ref = _first(msg, "ClipName", "Clip", "File", "InputFile")
    has_clip = 1 if clip_ref else 0

    return {
        "source": "birdnet",
        "source_ref": source_ref,
        "common_name": common_name,
        "scientific_name": msg.get("ScientificName"),
        "species_code": msg.get("SpeciesCode"),
        "confidence": _as_float(msg.get("Confidence")),
        "location": msg.get("SourceNode"),
        "start_time": start_time,
        "end_time": _birdnet_epoch(msg, key="EndTime"),
        "has_clip": has_clip,
        "has_snapshot": 0,
        "clip_ref": clip_ref,
        "snapshot_ref": None,
        "raw_json": json.dumps(msg)[:8000],
        "created_at": _now(),
    }


def handle_birdnet(payload: bytes) -> None:
    """Handle a BirdNET-Go detection message on the ``birdnet`` topic."""
    try:
        msg = json.loads(payload)
    except (ValueError, TypeError):
        log.warning("BirdNET-Go: could not decode payload")
        return
    row = build_birdnet_row(msg)
    if row is None:
        return
    db.upsert_detection(row)
    log.debug("BirdNET detection stored: %s (%s)", row["common_name"], row["source_ref"])


# ----------------------------------------------------------------------------- helpers

def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first(msg: dict, *keys: str) -> Optional[str]:
    for k in keys:
        v = msg.get(k)
        if v:
            return str(v)
    return None


def _birdnet_epoch(msg: dict, key: str = "BeginTime") -> Optional[float]:
    """Derive epoch seconds from BirdNET-Go's timestamp fields.

    Prefers the ISO ``BeginTime``/``EndTime``; falls back to combining the ``Date`` and
    ``Time`` fields (assumed local time).
    """
    iso = msg.get(key)
    if iso:
        parsed = _parse_iso(str(iso))
        if parsed is not None:
            return parsed
    if key == "BeginTime":
        date = msg.get("Date")
        clock = msg.get("Time")
        if date and clock:
            try:
                dt = datetime.strptime(f"{date} {clock}", "%Y-%m-%d %H:%M:%S")
                return dt.timestamp()
            except ValueError:
                return None
    return None


def _parse_iso(value: str) -> Optional[float]:
    # BirdNET-Go emits RFC3339 with offset, e.g. 2024-04-06T08:23:48.736132138+11:00.
    # Python's fromisoformat handles offsets but not nanosecond precision, so trim to 6.
    v = value.strip()
    if v in ("", "0001-01-01T00:00:00Z"):
        return None
    v = v.replace("Z", "+00:00")
    if "." in v:
        head, _, tail = v.partition(".")
        frac = tail
        offset = ""
        for sign in ("+", "-"):
            idx = tail.find(sign)
            if idx > 0:
                frac, offset = tail[:idx], tail[idx:]
                break
        frac = frac[:6]
        v = f"{head}.{frac}{offset}"
    try:
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None
