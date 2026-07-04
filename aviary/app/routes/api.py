"""JSON endpoints backing the Chart.js visualizations and live refresh."""

from __future__ import annotations

import time
from typing import Optional

from fastapi import APIRouter, Query
from fastapi.concurrency import run_in_threadpool

from .. import db, notify, species_info

router = APIRouter()


def _norm_source(source: Optional[str]) -> Optional[str]:
    return source if source in ("frigate", "birdnet") else None


@router.get("/summary")
def summary(source: Optional[str] = Query(None), days: int = Query(7, ge=1, le=3650)):
    src = _norm_source(source)
    since = time.time() - days * 86400
    return {
        "stats": db.summary_stats(source=src, since=since),
        "top_species": db.top_species(limit=10, source=src, since=since),
    }


@router.get("/per-day")
def per_day(
    source: Optional[str] = Query(None),
    days: int = Query(30, ge=1, le=3650),
    species: Optional[str] = Query(None),
    since: Optional[float] = Query(None, ge=0),
):
    data = db.detections_per_day(
        days=days, source=_norm_source(source), species=species, since=since
    )
    return {"data": data}


@router.get("/hourly")
def hourly(
    source: Optional[str] = Query(None),
    days: int = Query(30, ge=1, le=3650),
    species: Optional[str] = Query(None),
    since: Optional[float] = Query(None, ge=0),
):
    effective_since = since if since is not None else time.time() - days * 86400
    data = db.hourly_activity(
        source=_norm_source(source), since=effective_since, species=species
    )
    return {"data": data}


@router.get("/latest")
def latest(source: Optional[str] = Query(None), species: Optional[str] = Query(None)):
    """Cheap change marker polled by the Recent page for live refresh."""
    return db.change_marker(source=_norm_source(source), species=species)


@router.post("/test-notification")
async def test_notification():
    """Fire a test ``aviary_detection`` event so notifications can be verified
    without waiting for a real detection. Uses the latest detection (real image
    pipeline included), marked as a new species so the default blueprint settings
    notify; returns the delivery status for troubleshooting."""
    rows = await run_in_threadpool(db.recent_detections, 1)
    row = rows[0] if rows else {
        # Empty database: fire a synthetic audio detection with no image.
        "source": "birdnet",
        "source_ref": "aviary-test",
        "common_name": "Aviary Test Bird",
        "scientific_name": None,
        "confidence": 1.0,
        "location": "Aviary",
        "start_time": time.time(),
    }
    return await notify.send_detection(dict(row), is_new=True, test=True)


@router.get("/species-info")
async def species_info_endpoint(
    name: str = Query(..., min_length=1),
    sci: Optional[str] = Query(None),
):
    """Wikipedia blurb + iNaturalist taxonomy for a species (cached; lazy-loaded)."""
    return await species_info.resolve(name, sci)
