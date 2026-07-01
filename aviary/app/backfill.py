"""One-shot backfill of existing detections from Frigate and BirdNET-Go HTTP APIs.

Runs on startup (in the background) so the database is prepopulated with whatever each
source still retains, instead of only capturing new MQTT events going forward. Upserts go
through the same row-builders and unique index as the live MQTT path, so backfill is
idempotent and dedupes against live-ingested rows.
"""

from __future__ import annotations

import logging

import httpx

from . import ingest
from .settings import Settings

log = logging.getLogger("aviary.backfill")

# Per-page sizes (both APIs cap their page size; these stay at/below those caps).
_FRIGATE_PAGE = 200
_BIRDNET_PAGE = 1000
# Safety ceiling so a misbehaving API can't loop forever.
_MAX_PAGES = 500


async def run_backfill(settings: Settings) -> None:
    """Backfill both sources. Never raises — failures are logged and skipped."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        if settings.frigate_url:
            try:
                n = await _backfill_frigate(client, settings)
                log.info("Backfill: imported/updated %d Frigate detections.", n)
            except Exception:  # noqa: BLE001
                log.exception("Backfill: Frigate import failed (continuing).")
        if settings.birdnet_url:
            try:
                n = await _backfill_birdnet(client, settings)
                log.info("Backfill: imported/updated %d BirdNET-Go detections.", n)
            except Exception:  # noqa: BLE001
                log.exception("Backfill: BirdNET-Go import failed (continuing).")


# ------------------------------------------------------------------------- Frigate

async def _backfill_frigate(client: httpx.AsyncClient, settings: Settings) -> int:
    """Page backwards through GET /api/events (bird only) using a `before` cursor."""
    base = settings.frigate_url
    imported = 0
    before: float | None = None

    for _ in range(_MAX_PAGES):
        params = {
            "labels": "bird",
            "limit": _FRIGATE_PAGE,
            "include_thumbnails": 0,
        }
        if before is not None:
            params["before"] = before
        resp = await client.get(f"{base}/api/events", params=params)
        resp.raise_for_status()
        events = resp.json()
        if not isinstance(events, list) or not events:
            break

        min_start = None
        for ev in events:
            if ingest.store_row(ingest.build_frigate_row(ev)):
                imported += 1
            st = ev.get("start_time")
            if st is not None and (min_start is None or st < min_start):
                min_start = st

        if len(events) < _FRIGATE_PAGE or min_start is None:
            break
        # Next page: strictly older than the oldest event we just saw.
        next_before = min_start - 0.0001
        if before is not None and next_before >= before:
            break  # no progress; avoid an infinite loop
        before = next_before

    return imported


# ------------------------------------------------------------------------- BirdNET-Go

def birdnet_msg_from_api(d: dict) -> dict:
    """Map a BirdNET-Go /api/v2/detections object (camelCase) to the MQTT-style shape."""
    source = d.get("source") or {}
    return {
        "ID": d.get("id"),
        "CommonName": d.get("commonName"),
        "ScientificName": d.get("scientificName"),
        "SpeciesCode": d.get("speciesCode"),
        "Confidence": d.get("confidence"),
        "ClipName": d.get("clipName"),
        "BeginTime": d.get("beginTime"),
        "EndTime": d.get("endTime"),
        "Date": d.get("date"),
        "Time": d.get("time"),
        "SourceNode": source.get("displayName") or source.get("id"),
    }


async def _backfill_birdnet(client: httpx.AsyncClient, settings: Settings) -> int:
    """Page through GET /api/v2/detections (empty queryType lists all) via offset."""
    base = settings.birdnet_url
    imported = 0
    offset = 0

    for _ in range(_MAX_PAGES):
        params = {"numResults": _BIRDNET_PAGE, "offset": offset}
        resp = await client.get(f"{base}/api/v2/detections", params=params)
        resp.raise_for_status()
        payload = resp.json()

        # Response is a PaginatedResponse {data: [...], total, total_pages}; tolerate a
        # bare array too, in case of an older/different build.
        if isinstance(payload, dict):
            rows = payload.get("data") or []
            total = payload.get("total")
        else:
            rows = payload if isinstance(payload, list) else []
            total = None
        if not rows:
            break

        for d in rows:
            if ingest.store_row(ingest.build_birdnet_row(birdnet_msg_from_api(d))):
                imported += 1

        offset += len(rows)
        if len(rows) < _BIRDNET_PAGE:
            break
        if total is not None and offset >= total:
            break

    return imported
