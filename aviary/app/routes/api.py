"""JSON endpoints backing the Chart.js visualizations."""

from __future__ import annotations

import time
from typing import Optional

from fastapi import APIRouter, Query

from .. import db

router = APIRouter()


def _norm_source(source: Optional[str]) -> Optional[str]:
    return source if source in ("frigate", "birdnet") else None


@router.get("/summary")
def summary(source: Optional[str] = Query(None), days: int = Query(7)):
    src = _norm_source(source)
    since = time.time() - days * 86400 if days > 0 else None
    return {
        "stats": db.summary_stats(source=src, since=since),
        "top_species": db.top_species(limit=10, source=src, since=since),
    }


@router.get("/per-day")
def per_day(source: Optional[str] = Query(None), days: int = Query(30)):
    return {"data": db.detections_per_day(days=days, source=_norm_source(source))}


@router.get("/hourly")
def hourly(source: Optional[str] = Query(None), days: int = Query(30)):
    since = time.time() - days * 86400 if days > 0 else None
    return {"data": db.hourly_activity(source=_norm_source(source), since=since)}
