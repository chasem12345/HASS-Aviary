"""Kept-footage plumbing: Frigate's retain flag and exports, shared by 📌 keep and keepsakes.

📌 keep flips ``retain_indefinitely`` on the Frigate event. Since Frigate 0.14 that flag
protects the event itself — its row, snapshot and thumbnail — but NOT the recording
segments behind its clip: those expire with the alerts/detections retention regardless
(only Frigate's emergency low-disk purge still spares them). And on a paired setup the
zoomed view is the PTZ camera's continuous recordings, which no flag ever reached.
Frigate's one retention-exempt form of footage is an **export**, so pinning a
paired-camera event also exports the partner camera's window, and unpinning deletes the
export. The species keepsakes (``keepsakes.py``) use the same two primitives for the
first and latest sighting of every species. Shared here so every caller decides
identically.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Optional

import httpx

from . import db, proxy

log = logging.getLogger("aviary.kept")

# Fallback when the option is missing or malformed. Seconds added on each side of the
# event window when viewing a detection (both cameras) or exporting the kept zoomed
# footage. Frigate's event bounds are when the camera tracked the object; birds arrive
# before tracking starts and linger after it drops. One knob (clip_pad_seconds) so what
# you preview is exactly what gets kept.
_DEFAULT_PAD_S = 10.0

# Longest recordings window any player button or export may request. A malformed
# request must not make ffmpeg remux an hour of 4K; sized to fit a long event plus
# clip_pad_seconds at its maximum (300 each side). Shared by the media route (which
# rejects longer windows) and the visit card (which clamps to it, so a marathon visit
# plays its first 15 minutes instead of a 400).
RECORDING_MAX_S = 900.0


def view_pad(settings) -> float:
    """The configured clip_pad_seconds, defensively (settings may be a test stub)."""
    try:
        return max(0.0, float(getattr(settings, "clip_pad_seconds", _DEFAULT_PAD_S)))
    except (TypeError, ValueError):
        return _DEFAULT_PAD_S


def padded_window(start: float, end: float, pad: float) -> tuple[float, float]:
    """The event window widened by ``pad`` on both sides (never before epoch 0)."""
    return max(0.0, float(start) - pad), float(end) + pad


def kept_export_window(det: dict, settings) -> Optional[tuple[str, float, float]]:
    """(paired_camera, start, end) for a kept event's zoomed export, or None.

    None when the event's camera has no partner in the zoom map, or the event has no
    finished time window. The window is padded by clip_pad_seconds.
    """
    camera = (det.get("location") or "").strip().lower()
    other = settings.camera_pairs.get(camera)
    start, end = det.get("start_time"), det.get("end_time")
    if not other or not start or not end or end <= start:
        return None
    p_start, p_end = padded_window(start, end, view_pad(settings))
    return other, p_start, p_end


# What set_frigate_retain returns when Frigate no longer has the event at all.
GONE = "gone"


async def set_frigate_retain(settings, source_ref: str, keep: bool) -> Optional[str]:
    """Flip ``retain_indefinitely`` on a Frigate event. None on success, else an error.

    ``GONE`` means Frigate answered 404: the event is past retention and no longer
    exists there — nothing to keep, and (for a release) nothing left to release.
    """
    url = proxy.frigate_retain_url(settings.frigate_url, source_ref)
    try:
        status, text = await proxy.call_upstream("POST" if keep else "DELETE", url)
    except httpx.HTTPError as exc:
        return f"Frigate unreachable: {exc}"
    if status == 404:
        return GONE
    if status >= 400:
        return f"Frigate returned {status}: {text}"
    return None


async def frigate_has_event(settings, source_ref: str) -> Optional[bool]:
    """Whether Frigate still has the event (None when Frigate can't be asked)."""
    try:
        status, _ = await proxy.call_upstream(
            "GET", proxy.frigate_event_api_url(settings.frigate_url, source_ref))
    except httpx.HTTPError:
        return None
    if status == 404:
        return False
    if status >= 400:
        return None
    return True


def export_stamp(start: float) -> str:
    return datetime.fromtimestamp(start).strftime("%Y-%m-%d %H:%M")


async def export_window(settings, camera: str, start: float, end: float,
                        name: str) -> tuple[Optional[str], str]:
    """Ask Frigate to export ``camera``'s recordings for [start, end].

    Returns (export_id, status text). Asynchronous at Frigate (202 queued); callers
    resolve the finished file lazily. Status text is user-facing: "queued" or
    "failed: …" — Frigate rejects a window it has no recordings for with a 4xx, which
    is the normal outcome for history that has already expired.
    """
    try:
        status, text = await proxy.call_upstream(
            "POST", proxy.frigate_export_url(settings.frigate_url, camera, start, end),
            json={"source": "recordings", "name": name},
        )
    except httpx.HTTPError as exc:
        return None, f"failed: Frigate unreachable: {exc}"
    if status >= 400:
        return None, f"failed: Frigate returned {status}: {text}"
    try:
        export_id = json.loads(text).get("export_id")
    except (ValueError, TypeError, AttributeError):
        export_id = None
    if not export_id:
        # Pre-0.18 builds don't return the id; without it the export can't be tracked
        # or deleted, so fail loudly rather than orphaning exports silently.
        return None, "failed: Frigate did not return an export id (needs Frigate 0.18+)"
    return str(export_id), "queued"


async def create_kept_export(det: dict, settings) -> tuple[Optional[str], str]:
    """Export the paired camera's window at Frigate. Returns (export_id, status text).

    Status text is user-facing: "queued", "not applicable", or "failed: …".
    """
    window = kept_export_window(det, settings)
    if window is None:
        return None, "not applicable"
    camera, start, end = window
    name = f"Aviary · {det.get('common_name') or 'bird'} · {export_stamp(start)}"
    return await export_window(settings, camera, start, end, name)


async def delete_kept_export(export_id: str, settings) -> Optional[str]:
    """Remove a kept export at Frigate. Returns an error string, or None on success."""
    try:
        status, text = await proxy.call_upstream(
            "POST", proxy.frigate_exports_delete_url(settings.frigate_url),
            json={"ids": [export_id]},
        )
    except httpx.HTTPError as exc:
        return f"delete failed: Frigate unreachable: {exc}"
    if status >= 400:
        return f"delete failed: Frigate returned {status}: {text}"
    return None


async def backfill_exports(settings) -> None:
    """One-time retro-protection: export the zoomed window of already-kept events.

    Rows pinned before this feature have only their wide clip protected. Runs once
    (app_prefs marker); windows whose recordings already expired fail harmlessly —
    Frigate rejects the export and the row simply stays wide-only.
    """
    done = await asyncio.to_thread(db.get_pref, "kept_export_backfill_done")
    if done:
        return
    rows = await asyncio.to_thread(db.retained_missing_export)
    created = failed = 0
    for det in rows:
        if kept_export_window(det, settings) is None:
            continue
        export_id, status = await create_kept_export(det, settings)
        if export_id:
            await asyncio.to_thread(db.set_kept_export, det["id"], export_id)
            created += 1
        else:
            failed += 1
            log.info("Kept-export backfill for %s: %s", det.get("source_ref"), status)
    if created or failed:
        log.info(
            "Kept-export backfill: exported the zoomed window for %d kept event(s) "
            "(%d had no recordings left).", created, failed,
        )
    await asyncio.to_thread(db.set_pref, "kept_export_backfill_done", "1")
