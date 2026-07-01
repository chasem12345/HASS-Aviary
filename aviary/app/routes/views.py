"""HTML pages: dashboard, recent detections, species detail."""

from __future__ import annotations

import time
from typing import Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from .. import db
from . import templates

router = APIRouter()

_RANGE_SECONDS = {"today": None, "7d": 7 * 86400, "30d": 30 * 86400, "all": None}


def _since(range_key: str) -> Optional[float]:
    if range_key == "today":
        return time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    secs = _RANGE_SECONDS.get(range_key)
    return time.time() - secs if secs else None


def _norm_source(source: Optional[str]) -> Optional[str]:
    return source if source in ("frigate", "birdnet") else None


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    source: Optional[str] = Query(None),
    range: str = Query("7d"),
):
    src = _norm_source(source)
    since = _since(range)
    ctx = {
        "request": request,
        "page": "dashboard",
        "source": src or "all",
        "range": range if range in _RANGE_SECONDS else "7d",
        "stats": db.summary_stats(source=src, since=since),
        "leaders": db.top_species(limit=10, source=src, since=since),
    }
    return templates.TemplateResponse("dashboard.html", ctx)


@router.get("/recent", response_class=HTMLResponse)
def recent(
    request: Request,
    source: Optional[str] = Query(None),
    species: Optional[str] = Query(None),
):
    src = _norm_source(source)
    detections = db.recent_detections(limit=60, source=src, species=species)
    ctx = {
        "request": request,
        "page": "recent",
        "source": src or "all",
        "species": species,
        "detections": detections,
    }
    return templates.TemplateResponse("recent.html", ctx)


@router.get("/species/{name}", response_class=HTMLResponse)
def species_detail(request: Request, name: str):
    detections = db.recent_detections(limit=60, species=name)
    ctx = {
        "request": request,
        "page": "species",
        "species": name,
        "detections": detections,
    }
    return templates.TemplateResponse("species.html", ctx)
