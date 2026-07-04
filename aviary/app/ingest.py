"""Normalize Frigate and BirdNET-Go MQTT payloads into ``detections`` rows."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

from . import db, notify

log = logging.getLogger("aviary.ingest")

# When True, detections with no species (generic "bird") are dropped. Configured at
# startup via configure(); applies to both live MQTT ingest and HTTP backfill because
# both store rows through store_row().
_ignore_unclassified = False

# Notification state, guarded by one lock because store_row runs on both paho's MQTT
# thread and the backfill asyncio task:
#  - _known_species: species already in the DB (seeded at startup); anything not in
#    here is a "new species".
#  - _announced_refs: detections already announced as an aviary_detection event.
#    Frigate sends several MQTT messages per event (new/update/end) that upsert the
#    same row — announce each detection exactly once. Insertion-ordered dict trimmed
#    to a cap so memory stays bounded.
_known_species: set[str] = set()
_announced_refs: dict[str, None] = {}
_ANNOUNCED_CAP = 4096
_known_lock = threading.Lock()
_loop: Optional[asyncio.AbstractEventLoop] = None


def configure(ignore_unclassified: bool) -> None:
    global _ignore_unclassified
    _ignore_unclassified = ignore_unclassified


def seed_notify_state() -> None:
    """Seed known species (all-time) and recently announced refs from the DB.

    Recent refs are pre-marked announced so an in-progress Frigate event doesn't
    re-notify after an add-on restart.
    """
    with _known_lock:
        _known_species.update(db.distinct_species())
        for source, ref in db.recent_refs(time.time() - 3600):
            _announced_refs[f"{source}:{ref}"] = None
        species, refs = len(_known_species), len(_announced_refs)
    log.info("Seeded notification state: %d known species, %d recent refs.", species, refs)


def set_event_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Capture the app's event loop so the MQTT thread can schedule notifications."""
    global _loop
    _loop = loop


def is_unclassified(row: dict) -> bool:
    return (row.get("common_name") or "").strip().lower() == "bird"


def store_row(row: Optional[dict], live: bool = True) -> bool:
    """Upsert a built row unless it's filtered out. Returns True if stored.

    ``live=False`` (backfill) still records the species/refs as seen but never fires
    detection events — historical rows are not news.
    """
    if row is None:
        return False
    if _ignore_unclassified and is_unclassified(row):
        log.debug("Skipping unclassified %s detection (%s)", row["source"], row["source_ref"])
        return False
    db.upsert_detection(row)
    _announce(row, live)
    return True


def _announce(row: dict, live: bool) -> None:
    """Fire an aviary_detection event once per classified detection.

    An unclassified row is skipped here but NOT marked announced, so a Frigate event
    that gains its ``sub_label`` on a later message gets announced at that point —
    exactly when the species becomes known.
    """
    if is_unclassified(row):  # generic 'bird' is never a species
        return
    name = row["common_name"]
    key = f"{row['source']}:{row['source_ref']}"
    with _known_lock:
        if key in _announced_refs:
            return
        _announced_refs[key] = None
        while len(_announced_refs) > _ANNOUNCED_CAP:
            _announced_refs.pop(next(iter(_announced_refs)))
        is_new = name not in _known_species
        if is_new:
            _known_species.add(name)
    # Accepted race: a live detection during a first-run backfill can announce a
    # species/ref the backfill was about to import — genuinely first-seen by Aviary.
    if live and _loop is not None and notify.enabled():
        asyncio.run_coroutine_threadsafe(notify.send_detection(dict(row), is_new=is_new), _loop)


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
    # First non-None wins (an `or` chain would drop a legitimate 0.0 score).
    confidence = _as_float(_first_not_none(
        obj.get("top_score"),
        obj.get("score"),
        data.get("top_score"),
        data.get("score"),
    ))

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
        "native_id": str(event_id),
        "raw_json": _raw_json(obj),
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
    if store_row(row):
        log.debug("Frigate detection upserted: %s (%s)", row["common_name"], row["source_ref"])


# ------------------------------------------------------------------------- BirdNET-Go

def build_birdnet_row(msg: dict) -> Optional[dict]:
    """Build a detections row from a BirdNET-Go detection.

    Expects the MQTT payload shape: PascalCase ``datastore.Note`` keys (``CommonName``,
    ``ScientificName``, ``SpeciesCode``, ``Confidence``, ``ClipName``, ``BeginTime``,
    ``EndTime``, ``Date``, ``Time``, ``SourceNode``) plus the camelCase ``detectionId``
    (the database id, present on current builds; older ones sent ``ID``). The HTTP-API
    backfill maps its camelCase response into this shape first (see
    ``backfill.birdnet_msg_from_api``), so both paths produce identical ``source_ref``
    values and dedupe cleanly.
    """
    if not isinstance(msg, dict):
        return None

    common_name = msg.get("CommonName") or "bird"
    start_time = _birdnet_epoch(msg) or _now()

    # The database id arrives as camelCase `detectionId` on current BirdNET-Go builds
    # (top-level `ID` was removed from the MQTT payload; on v0.6.4 it was always 0).
    # The HTTP backfill maps its `id` into `ID`. Both therefore key identically here,
    # so live and backfilled rows dedupe. Without an id, fall back to species+second,
    # which collapses duplicate publishes of the same detection.
    raw_id = _first_not_none(msg.get("detectionId"), msg.get("ID"))
    has_native_id = raw_id not in (None, 0, "", "0")
    source_ref = (
        f"{raw_id}-{int(start_time)}" if has_native_id
        else f"{common_name}-{int(start_time)}"
    )

    # Audio clip locator: BirdNET-Go payloads may include a clip filename under one of a
    # few keys depending on version. Store whatever is present; the proxy resolves it
    # against birdnet_url. With a native id the by-id audio endpoint works even
    # without a filename.
    clip_ref = _first(msg, "ClipName", "Clip", "File", "InputFile")
    has_clip = 1 if (clip_ref or has_native_id) else 0

    return {
        "source": "birdnet",
        "source_ref": source_ref,
        "common_name": common_name,
        "scientific_name": msg.get("ScientificName"),
        "species_code": msg.get("SpeciesCode"),
        "confidence": _as_float(msg.get("Confidence")),
        "location": _first(msg, "SourceNode", "sourceName"),
        "start_time": start_time,
        "end_time": _birdnet_epoch(msg, key="EndTime"),
        "has_clip": has_clip,
        "has_snapshot": 0,
        "clip_ref": clip_ref,
        "snapshot_ref": None,
        # BirdNET-Go's own database id — lets the media proxy use the by-id API
        # endpoints (audio/spectrogram) instead of guessing clip file paths.
        "native_id": str(raw_id) if has_native_id else None,
        "raw_json": _raw_json(msg),
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
    if store_row(row):
        log.debug("BirdNET detection stored: %s (%s)", row["common_name"], row["source_ref"])


# ----------------------------------------------------------------------------- helpers

def _first_not_none(*values: Any) -> Any:
    for v in values:
        if v is not None:
            return v
    return None


def _raw_json(obj: Any, limit: int = 8000) -> Optional[str]:
    """Serialize the source payload for debugging; never store truncated/invalid JSON.

    Oversized payloads (e.g. Frigate events with bulky path/region data) get their
    largest values dropped until the document fits; if it still doesn't fit, store
    nothing rather than a sliced document.
    """
    try:
        text = json.dumps(obj)
    except (TypeError, ValueError):
        return None
    if len(text) <= limit:
        return text
    if isinstance(obj, dict):
        slim = dict(obj)
        # Drop the largest values first until it fits.
        for k in sorted(slim, key=lambda k: len(str(slim[k])), reverse=True):
            slim.pop(k)
            text = json.dumps(slim)
            if len(text) <= limit:
                return text
    return None


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
