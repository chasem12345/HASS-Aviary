"""Kept-footage plumbing: preserve the ZOOMED half of a pinned two-camera event.

📌 keep flips ``retain_indefinitely`` on the Frigate event, which protects the event's
own (wide) clip — but on a paired setup the zoomed view is the PTZ camera's continuous
recordings, which no retain flag can reach: they expire with ``record.retain.days``
regardless. Frigate's only retention-exempt form for non-event footage is an **export**,
so pinning a paired-camera event also exports the partner camera's window, and unpinning
deletes the export. Shared between the retain endpoint and the one-time startup backfill
so both decide identically.
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

# Seconds added on each side of the event window when viewing or exporting the paired
# camera's footage. Frigate's event bounds are when the WIDE camera tracked the object;
# the PTZ is still travelling at the start and the bird often lingers after tracking
# drops. One constant so what you preview (⇄) and what gets kept (export) are the same
# window. Also applied to the card's data attributes via the template context.
WINDOW_PAD_S = 30.0


def padded_window(start: float, end: float) -> tuple[float, float]:
    """The event window widened by WINDOW_PAD_S on both sides (never before epoch 0)."""
    return max(0.0, float(start) - WINDOW_PAD_S), float(end) + WINDOW_PAD_S


def kept_export_window(det: dict, settings) -> Optional[tuple[str, float, float]]:
    """(paired_camera, start, end) for a kept event's zoomed export, or None.

    None when the event's camera has no partner in the zoom map, or the event has no
    finished time window. The window is padded (see WINDOW_PAD_S).
    """
    camera = (det.get("location") or "").strip().lower()
    other = settings.camera_pairs.get(camera)
    start, end = det.get("start_time"), det.get("end_time")
    if not other or not start or not end or end <= start:
        return None
    p_start, p_end = padded_window(start, end)
    return other, p_start, p_end


async def create_kept_export(det: dict, settings) -> tuple[Optional[str], str]:
    """Export the paired camera's window at Frigate. Returns (export_id, status text).

    Asynchronous at Frigate (202 queued); the media route resolves the finished file
    lazily. Status text is user-facing: "queued", "not applicable", or "failed: …".
    """
    window = kept_export_window(det, settings)
    if window is None:
        return None, "not applicable"
    camera, start, end = window
    stamp = datetime.fromtimestamp(start).strftime("%Y-%m-%d %H:%M")
    name = f"Aviary · {det.get('common_name') or 'bird'} · {stamp}"
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
