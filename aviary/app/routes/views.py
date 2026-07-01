"""HTML pages: dashboard, recent detections, species detail."""

from __future__ import annotations

import time
from datetime import date, timedelta
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from .. import db
from . import ingress_url, templates

router = APIRouter()

PAGE_SIZE = 48

_RANGE_SECONDS = {"today": None, "7d": 7 * 86400, "30d": 30 * 86400, "all": None}
# Fallback day-span per range for chart endpoints (per-day chart x-axis width).
_RANGE_DAYS = {"today": 1, "7d": 7, "30d": 30, "all": 3650}


def _norm_range(range_key: str, default: str = "7d") -> str:
    return range_key if range_key in _RANGE_SECONDS else default


def _since(range_key: str) -> Optional[float]:
    if range_key == "today":
        # Local midnight, not a rolling 24h window.
        return time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    secs = _RANGE_SECONDS.get(range_key)
    return time.time() - secs if secs else None


def _norm_source(source: Optional[str]) -> Optional[str]:
    return source if source in ("frigate", "birdnet") else None


def _day_groups(detections: list[dict]) -> list[dict]:
    """Group newest-first detections into contiguous local-day buckets."""
    today = date.today()
    yesterday = today - timedelta(days=1)
    groups: list[dict] = []
    for det in detections:
        try:
            day = date.fromtimestamp(float(det["start_time"]))
        except (TypeError, ValueError, OSError, OverflowError):
            day = today
        if day == today:
            label = "Today"
        elif day == yesterday:
            label = "Yesterday"
        else:
            label = f"{day.strftime('%B')} {day.day}"
            if day.year != today.year:
                label += f", {day.year}"
        if not groups or groups[-1]["label"] != label:
            groups.append({"label": label, "items": []})
        groups[-1]["items"].append(det)
    return groups


def _paged(
    source: Optional[str],
    species: Optional[str],
    before: Optional[float],
    since: Optional[float],
) -> tuple[list[dict], Optional[float]]:
    """One page of detections plus the next ``before`` cursor (None = no more)."""
    rows = db.recent_detections(
        limit=PAGE_SIZE + 1, source=source, species=species, before=before, since=since
    )
    has_more = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]
    next_before = rows[-1]["start_time"] if has_more and rows else None
    return rows, next_before


# ------------------------------------------------------------------------------- pages

@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    source: Optional[str] = Query(None),
    range_key: str = Query("7d", alias="range"),
):
    src = _norm_source(source)
    range_key = _norm_range(range_key)
    since = _since(range_key)

    leaders = db.top_species(limit=10, source=src, since=since)
    latest = db.recent_detections(limit=1, source=src)
    ingestor = getattr(request.app.state, "ingestor", None)

    ctx = {
        "request": request,
        "page": "dashboard",
        "source": src or "all",
        "range": range_key,
        # Charts use the same boundary as the stat cards ("today" = local midnight).
        "chart_since": since or "",
        "chart_days": _RANGE_DAYS[range_key],
        "stats": db.summary_stats(source=src, since=since),
        "leaders": leaders,
        "thumbs": db.latest_snapshot_refs([s["common_name"] for s in leaders]),
        "latest": latest[0] if latest else None,
        "mqtt_enabled": request.app.state.settings.mqtt_enabled,
        "mqtt_connected": bool(ingestor and ingestor.connected),
    }
    return templates.TemplateResponse("dashboard.html", ctx)


def _recent_ctx(
    request: Request,
    source: Optional[str],
    species: Optional[str],
    range_key: str,
    before: Optional[float],
) -> dict:
    src = _norm_source(source)
    range_key = _norm_range(range_key, default="all")
    since = _since(range_key)
    detections, next_before = _paged(src, species, before, since)
    older_url = None
    if next_before is not None:
        q: dict = {"before": f"{next_before:.6f}"}
        if src:
            q["source"] = src
        if species:
            q["species"] = species
        if range_key != "all":
            q["range"] = range_key
        older_url = f"{ingress_url(request, 'recent')}?{urlencode(q)}"
    return {
        "request": request,
        "page": "recent",
        "source": src or "all",
        "species": species,
        "range": range_key,
        "groups": _day_groups(detections),
        "next_before": next_before,
        "older_url": older_url,
        "paged": before is not None,
        "species_options": db.distinct_species(),
        "newest": detections[0]["start_time"] if detections else 0,
    }


@router.get("/recent", response_class=HTMLResponse)
def recent(
    request: Request,
    source: Optional[str] = Query(None),
    species: Optional[str] = Query(None),
    range_key: str = Query("all", alias="range"),
    before: Optional[float] = Query(None),
):
    ctx = _recent_ctx(request, source, species, range_key, before)
    return templates.TemplateResponse("recent.html", ctx)


@router.get("/recent/partial", response_class=HTMLResponse)
def recent_partial(
    request: Request,
    source: Optional[str] = Query(None),
    species: Optional[str] = Query(None),
    range_key: str = Query("all", alias="range"),
    before: Optional[float] = Query(None),
    highlight_after: Optional[float] = Query(None),
):
    """Server-rendered detection groups for the Recent page's live refresh."""
    ctx = _recent_ctx(request, source, species, range_key, before)
    ctx["highlight_after"] = highlight_after
    return templates.TemplateResponse("_groups.html", ctx)


@router.get("/species/{name}", response_class=HTMLResponse)
def species_detail(
    request: Request,
    name: str,
    before: Optional[float] = Query(None),
):
    detections, next_before = _paged(None, name, before, None)
    older_url = None
    if next_before is not None:
        base = ingress_url(request, "species_detail", name=name)
        older_url = f"{base}?{urlencode({'before': f'{next_before:.6f}'})}"
    ctx = {
        "request": request,
        "page": "species",
        "species": name,
        "stats": db.species_stats(name),
        "thumb": db.latest_snapshot_refs([name]).get(name),
        "groups": _day_groups(detections),
        "next_before": next_before,
        "older_url": older_url,
        "paged": before is not None,
    }
    return templates.TemplateResponse("species.html", ctx)
